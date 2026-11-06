from __future__ import annotations
import base64
import binascii
from .contract import key_token, make_plan, verify_candidate
from .evidence import bundle, sha256_bytes
from .provider import ProviderError


class InspectionError(ValueError):
    def __init__(self, message: str, *, run: dict | None = None):
        super().__init__(message)
        self.run = run


def decode_file(encoded: str) -> bytes:
    if not isinstance(encoded, str) or len(encoded) > 350000:
        raise InspectionError("Choose SQLite snapshots up to 256 KiB each.")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise InspectionError("The database file could not be decoded.") from error
    if not 512 <= len(raw) <= 256 * 1024 or not raw.startswith(b"SQLite format 3\x00"):
        raise InspectionError("Choose SQLite snapshots up to 256 KiB each.")
    return raw


def inspect_input(raw: bytes, sandbox) -> tuple[dict, dict]:
    run = sandbox.run("inspect", {"current.sqlite": raw})
    if not run["ok"]:
        raise InspectionError(run.get("error", "The database did not pass isolated inspection."), run=public_run(run))
    snapshot = run["payload"]
    if snapshot.get("file_sha256") != sha256_bytes(raw):
        raise InspectionError("The inspected database fingerprint does not match the uploaded file.", run=public_run(run))
    return snapshot, public_run(run)


def public_run(run: dict) -> dict:
    return {key: value for key, value in run.items() if key != "payload"}


def run_recovery(*, backup: dict, current: dict, backup_raw: bytes, current_raw: bytes,
                 plan: dict, prior_runs: list[dict], sandbox, provider, progress=lambda stage, message: None):
    bound = make_plan(backup, current, plan["scope"]["columns"], [key_token(key) for key in plan["scope"]["keys"]])
    if (bound["sha256"] != plan["sha256"] or sha256_bytes(backup_raw) != plan["backup_sha256"]
            or sha256_bytes(current_raw) != plan["current_sha256"]):
        raise InspectionError("The selected recovery or source files changed. Inspect them again.")
    runs, attempts = list(prior_runs), []
    previous, failures, artifact, verification = None, None, None, None
    status, message = "failed", "The proposed recovery did not pass its independent checks."
    for number in (1, 2):
        progress("generating", "Vultr AI is writing a recovery for the cells you selected." if number == 1 else "The first recovery was rejected. Vultr AI is correcting its program.")
        try:
            candidate = provider.propose(current, plan, previous=previous, failures=failures)
        except ProviderError as error:
            message = str(error)
            break
        attempt = {"number": number, "source_sha256": candidate["sha256"], "code": candidate["code"],
                   "explanation": candidate["explanation"], "provider": candidate["provider"], "data_sent": candidate["data_sent"]}
        attempts.append(attempt)
        progress("running", "The recovery program is running on disposable copies. Your original files stay unchanged.")
        execution = sandbox.run("candidate", {"backup.sqlite": backup_raw, "current.sqlite": current_raw,
                                               "repair.py": candidate["code"].encode()}, scope=plan["scope"])
        runs.append(public_run(execution))
        if not execution.get("policy_verified") or not execution.get("teardown", {}).get("confirmed"):
            message = "Sandbox isolation or removal could not be confirmed. No recovery can be downloaded."
            break
        if not execution["ok"]:
            attempt["rejected"] = execution.get("error", "The candidate did not finish.")
            previous, failures = candidate["code"], ["candidate-output"]
            continue
        try:
            proposed = decode_file(execution["payload"]["artifact"])
        except (InspectionError, KeyError):
            attempt["rejected"] = "The candidate did not produce a valid bounded database artifact."
            previous, failures = candidate["code"], ["candidate-output"]
            continue
        progress("checking", "A separate checker is reading the result and comparing every cell.")
        inspection = sandbox.run("inspect", {"current.sqlite": proposed})
        runs.append(public_run(inspection))
        if not inspection.get("policy_verified") or not inspection.get("teardown", {}).get("confirmed"):
            message = "Independent inspection isolation or removal could not be confirmed."
            break
        if not inspection["ok"] or inspection["payload"].get("file_sha256") != sha256_bytes(proposed):
            attempt["rejected"] = "The candidate database did not pass independent integrity and fingerprint checks."
            previous, failures = candidate["code"], ["sqlite-integrity"]
            continue
        verification = verify_candidate(backup, current, inspection["payload"], plan)
        attempt["verification"] = verification
        if verification["ok"] and all(run.get("policy_verified") and run.get("teardown", {}).get("confirmed") for run in runs):
            artifact, status = proposed, "verified"
            message = "The selected cells are restored. Every other value keeps today's data."
            break
        previous = candidate["code"]
        failures = [check["id"] for check in verification["checks"] if not check["ok"]]
        attempt["rejected"] = "Independent checks rejected these changes: " + ", ".join(failures)
    progress("finishing", "Confirming that every temporary sandbox was removed.")
    payload = {"project": "Rewind", "status": status, "message": message, "plan": plan,
               "verification": verification, "attempts": attempts, "sandbox_runs": runs,
               "artifact_sha256": sha256_bytes(artifact) if artifact is not None else None,
               "original_inputs_unchanged": sha256_bytes(backup_raw) == plan["backup_sha256"] and sha256_bytes(current_raw) == plan["current_sha256"],
               "all_sandboxes_removed": all(run.get("teardown", {}).get("confirmed") for run in runs),
               "host_attestation": False, "customer_use": "This software demonstration does not claim independent customer use."}
    return bundle(payload), artifact
