from __future__ import annotations
import base64
import binascii
from .contract import ScopeError, diagnostic_metadata, rebind_plan, validate_diagnosis, verify_candidate
from .evidence import bundle, canonical, sha256_bytes
from .provider import ProviderError


class InspectionError(ValueError):
    def __init__(self, message: str, *, run: dict | None = None):
        super().__init__(message)
        self.run = run


def decode_file(encoded: str, format: str = "sqlite") -> bytes:
    if format not in {"sqlite", "csv"} or not isinstance(encoded, str) or len(encoded) > 350000:
        raise InspectionError("Choose supported files up to 256 KiB each.")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise InspectionError("The file could not be decoded.") from error
    if not (1 if format == "csv" else 512) <= len(raw) <= 256 * 1024:
        raise InspectionError("Choose files up to 256 KiB each.")
    if format == "sqlite" and not raw.startswith(b"SQLite format 3\x00"):
        raise InspectionError("Choose consistent SQLite snapshots, or select CSV as the file format.")
    return raw


def inspect_input(raw: bytes, sandbox, *, format: str = "sqlite", primary_key: str = "") -> tuple[dict, dict]:
    run = sandbox.run("inspect", {"current." + format: raw}, scope={"format": format, "primary_key": primary_key})
    if not run["ok"]:
        raise InspectionError(run.get("error", "The file did not pass isolated inspection."), run=public_run(run))
    snapshot = run["payload"]
    if snapshot.get("file_sha256") != sha256_bytes(raw):
        raise InspectionError("The inspected fingerprint does not match the uploaded file.", run=public_run(run))
    return snapshot, public_run(run)


def public_run(run: dict) -> dict:
    return {key: value for key, value in run.items() if key != "payload"}


def removed_and_isolated(runs: list[dict]) -> bool:
    return bool(runs) and all(run.get("policy_verified") and run.get("teardown", {}).get("confirmed") for run in runs)


def run_diagnosis(*, backup: dict, checkpoint: dict, current: dict, backup_raw: bytes, checkpoint_raw: bytes,
                  current_raw: bytes, instruction: str, prior_runs: list[dict], sandbox, provider,
                  progress=lambda stage, message: None, consume_call=lambda: None) -> tuple[dict, list[dict]]:
    if not removed_and_isolated(prior_runs):
        raise InspectionError("Input inspection isolation and removal must be confirmed before investigation.")
    for snapshot, raw in [(backup, backup_raw), (checkpoint, checkpoint_raw), (current, current_raw)]:
        if snapshot["file_sha256"] != sha256_bytes(raw):
            raise InspectionError("An input changed. Inspect all three versions again.")
    metadata = diagnostic_metadata(backup, checkpoint, current)
    progress("diagnosing", "Vultr AI is writing an investigation for the mistake you described.")
    consume_call()
    try:
        candidate = provider.diagnose(metadata, instruction)
    except ProviderError as error:
        return {"status": "failed", "message": str(error), "model_calls": 1}, list(prior_runs)
    attempt = {**candidate, "source_sha256": candidate["sha256"]}
    progress("investigating", "The investigation is comparing all three versions in its own isolated environment.")
    format = current.get("format", "sqlite")
    context = {"format": format, "table": current["table"], "primary_key": current["primary_key"],
               "columns": [column["name"] for column in current["columns"]], "instruction": instruction}
    execution = sandbox.run("diagnose", {"backup." + format: backup_raw, "checkpoint." + format: checkpoint_raw,
                                         "current." + format: current_raw, "diagnose.py": candidate["code"].encode()}, scope=context)
    runs = [*prior_runs, public_run(execution)]
    if not removed_and_isolated(runs):
        raise InspectionError("Investigation isolation or removal could not be confirmed.", run=public_run(execution))
    diagnosis = {"status": "failed", "model_calls": 1, "attempt": attempt, "sandbox_run": public_run(execution)}
    if not execution["ok"]:
        diagnosis["message"] = "The investigation did not finish. You can select the damaged fields yourself."
        return diagnosis, runs
    progress("diagnostic-checking", "Checking the proposed cells against the independently inspected versions.")
    try:
        proposal = validate_diagnosis(backup, checkpoint, current, execution["payload"])
        # Reject an unreviewable scope before presenting it as ready to approve.
        if proposal["targets"]:
            rebind_plan(backup, current, {"targets": proposal["targets"]}, checkpoint)
    except (ScopeError, TypeError, KeyError, ValueError):
        diagnosis["message"] = "The investigation proposed an unsupported repair. Choose the damaged fields yourself."
        return diagnosis, runs
    diagnosis.update({"status": "proposed", "proposal": proposal,
                      "message": str(len(proposal["targets"])) + " cells can be restored; " + str(len(proposal["overlaps"])) + " overlapping later edits will stay."})
    return diagnosis, runs


def run_recovery(*, backup: dict, current: dict, backup_raw: bytes, current_raw: bytes,
                 plan: dict, prior_runs: list[dict], sandbox, provider, checkpoint: dict | None = None,
                 checkpoint_raw: bytes | None = None, diagnosis: dict | None = None, max_attempts: int = 2,
                 model_calls_prior: int = 0, progress=lambda stage, message: None, consume_call=lambda: None):
    if not 1 <= max_attempts <= 2 or model_calls_prior + max_attempts > 2:
        raise InspectionError("The two-call model budget cannot be exceeded.")
    bound = rebind_plan(backup, current, plan, checkpoint)
    inputs_unchanged = (sha256_bytes(backup_raw) == plan["backup_sha256"] and sha256_bytes(current_raw) == plan["current_sha256"]
                        and (checkpoint is None and plan.get("checkpoint_sha256") is None
                             or checkpoint is not None and checkpoint_raw is not None and sha256_bytes(checkpoint_raw) == plan["checkpoint_sha256"]))
    if (bound["sha256"] != plan["sha256"] or not inputs_unchanged
            or sha256_bytes(canonical({key: value for key, value in plan.items() if key != "sha256"})) != plan["sha256"]):
        raise InspectionError("The approved cells or source files changed. Inspect them again.")
    if not removed_and_isolated(prior_runs):
        raise InspectionError("Prior inspection isolation or removal could not be confirmed.")
    runs, attempts = list(prior_runs), []
    previous, failures, artifact, verification = None, None, None, None
    calls = 0
    status, message = "failed", "The proposed recovery did not pass its independent checks."
    format = current.get("format", "sqlite")
    for number in range(1, max_attempts + 1):
        progress("generating", "Vultr AI is writing the exact recovery you approved." if number == 1 else "The first recovery was rejected. Vultr AI is correcting its program.")
        consume_call()
        calls += 1
        try:
            candidate = provider.propose(current, plan, previous=previous, failures=failures)
        except ProviderError as error:
            attempts.append({"number": number, "request_started": True, "error": str(error)})
            message = str(error)
            break
        attempt = {"number": number, "source_sha256": candidate["sha256"], "code": candidate["code"],
                   "explanation": candidate["explanation"], "provider": candidate["provider"], "data_sent": candidate["data_sent"]}
        attempts.append(attempt)
        progress("running", "The approved recovery is running on disposable copies.")
        execution = sandbox.run("candidate", {"backup." + format: backup_raw, "current." + format: current_raw,
                                               "repair.py": candidate["code"].encode()}, scope=plan["scope"])
        runs.append(public_run(execution))
        if not removed_and_isolated(runs):
            message = "Sandbox isolation or removal could not be confirmed. No recovery can be downloaded."
            break
        if not execution["ok"]:
            attempt["rejected"] = execution.get("error", "The candidate did not finish.")
            previous, failures = candidate["code"], ["candidate-output"]
            continue
        try:
            proposed = decode_file(execution["payload"]["artifact"], format)
        except (InspectionError, KeyError, TypeError):
            attempt["rejected"] = "The candidate did not produce a valid bounded file."
            previous, failures = candidate["code"], ["candidate-output"]
            continue
        progress("checking", "A fresh checker is reading the result and comparing every cell.")
        inspection = sandbox.run("inspect", {"current." + format: proposed},
                                  scope={"format": format, "primary_key": current["primary_key"]})
        runs.append(public_run(inspection))
        if not removed_and_isolated(runs):
            message = "Independent inspection isolation or removal could not be confirmed."
            break
        if not inspection["ok"] or inspection["payload"].get("file_sha256") != sha256_bytes(proposed):
            attempt["rejected"] = "The recovered file did not pass independent structure and fingerprint checks."
            previous, failures = candidate["code"], ["file-integrity"]
            continue
        verification = verify_candidate(backup, current, inspection["payload"], plan)
        attempt["verification"] = verification
        if verification["ok"]:
            artifact, status = proposed, "verified"
            message = "The approved cells are restored. Every other cell keeps today's value."
            break
        previous = candidate["code"]
        failures = [check["id"] for check in verification["checks"] if not check["ok"]]
        attempt["rejected"] = "Independent checks rejected these changes: " + ", ".join(failures)
    progress("finishing", "Confirming that every temporary environment was removed.")
    payload = {"project": "Rewind", "status": status, "message": message, "format": format, "plan": plan,
               "diagnosis": diagnosis, "verification": verification, "attempts": attempts, "model_calls": model_calls_prior + calls,
               "sandbox_runs": runs, "artifact_sha256": sha256_bytes(artifact) if artifact is not None else None,
               "original_inputs_unchanged": inputs_unchanged,
               "all_sandboxes_removed": all(run.get("teardown", {}).get("confirmed") for run in runs),
               "host_attestation": False}
    return bundle(payload), artifact
