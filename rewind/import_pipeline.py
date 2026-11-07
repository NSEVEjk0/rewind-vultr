"""Produce and independently verify an approved CSV import in gVisor."""
from __future__ import annotations
from .evidence import bundle, canonical, sha256_bytes
from .pipeline import InspectionError, decode_file, inspect_input, public_run, removed_and_isolated
from .preflight import IMPORT_CONTRACT, rebind_import_plan, verify_import


def run_preflight(*, current: dict, incoming: dict, current_raw: bytes, incoming_raw: bytes,
                  plan: dict, prior_runs: list[dict], sandbox, progress=lambda stage, message: None):
    if (rebind_import_plan(current, incoming, plan) != plan
            or sha256_bytes(canonical({key: value for key, value in plan.items() if key != "sha256"})) != plan["sha256"]
            or sha256_bytes(current_raw) != plan["current_sha256"]
            or sha256_bytes(incoming_raw) != plan["incoming_sha256"]):
        raise InspectionError("The approved import or uploaded files changed. Review a fresh export.")
    if not removed_and_isolated(prior_runs):
        raise InspectionError("Input inspection isolation and removal must pass before an import preview.")
    runs = list(prior_runs)
    progress("running", "Building an import containing only your approved updates in an isolated environment.")
    prepared = sandbox.run("prepare-import", {"current.csv": current_raw, "incoming.csv": incoming_raw}, scope=plan["scope"])
    runs.append(public_run(prepared))
    if not prepared["ok"] or not removed_and_isolated(runs):
        raise InspectionError("The approved import could not be produced with confirmed isolation and removal.", run=public_run(prepared))
    try:
        approved_raw = decode_file(prepared["payload"]["artifact"], "csv")
    except (InspectionError, KeyError, TypeError) as error:
        raise InspectionError("The approved import did not return a bounded CSV file.", run=public_run(prepared)) from error
    progress("checking", "A separate checker is inspecting the approved import before the dry run.")
    approved, inspected_run = inspect_input(approved_raw, sandbox, format="csv", primary_key=current["primary_key"])
    runs.append(inspected_run)
    if not removed_and_isolated(runs):
        raise InspectionError("The approved import inspection isolation or removal failed.", run=inspected_run)
    progress("running", "Running the approved import on a disposable copy of today's export.")
    execution = sandbox.run("import-preview", {"current.csv": current_raw, "incoming.csv": approved_raw},
                            scope={"format": "csv", "primary_key": current["primary_key"], "contract": IMPORT_CONTRACT})
    runs.append(public_run(execution))
    if not execution["ok"] or not removed_and_isolated(runs):
        raise InspectionError("The import preview could not finish with confirmed isolation and removal.", run=public_run(execution))
    try:
        preview_raw = decode_file(execution["payload"]["artifact"], "csv")
    except (InspectionError, KeyError, TypeError) as error:
        raise InspectionError("The import preview did not produce a bounded CSV file.", run=public_run(execution)) from error
    progress("checking", "A fresh checker is comparing every resulting cell with today's export and your approval.")
    preview, inspected_run = inspect_input(preview_raw, sandbox, format="csv", primary_key=current["primary_key"])
    runs.append(inspected_run)
    if not removed_and_isolated(runs):
        raise InspectionError("The final preview inspection isolation or removal failed.", run=inspected_run)
    verification = verify_import(current, incoming, approved, preview, plan)
    progress("finishing", "Confirming all import checks and temporary environment removal.")
    payload = {"project": "Rewind", "workflow": "preflight", "contract": IMPORT_CONTRACT, "format": "csv",
               "status": "verified" if verification["ok"] else "failed",
               "message": "Your approved import passed. Every other value keeps today's export value." if verification["ok"]
                          else "The import failed its independent checks. No import can be downloaded.",
               "plan": plan, "verification": verification, "sandbox_runs": runs, "model_calls": 0,
               "approved_import_sha256": sha256_bytes(approved_raw) if verification["ok"] else None,
               "artifact_sha256": sha256_bytes(preview_raw) if verification["ok"] else None,
               "original_inputs_unchanged": sha256_bytes(current_raw) == current["file_sha256"]
                                            and sha256_bytes(incoming_raw) == incoming["file_sha256"],
               "all_sandboxes_removed": removed_and_isolated(runs), "host_attestation": False}
    return bundle(payload), approved_raw if verification["ok"] else None, preview_raw if verification["ok"] else None
