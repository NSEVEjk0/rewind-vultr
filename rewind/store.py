from __future__ import annotations
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import tempfile
import threading
import time
from .evidence import sha256_bytes

ACTIVE = {"inspecting", "generating", "running", "checking", "finishing", "queued",
          "diagnosing", "investigating", "diagnostic-checking"}


class AdmissionDenied(ValueError):
    pass


def atomic_json(path: Path, value: dict):
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.admission_path = self.root / "admission.json"
        if not self.admission_path.exists():
            atomic_json(self.admission_path, {"day": int(time.time()) // 86400, "uploads": 0, "recoveries": 0})
        # Never resume an interrupted model request automatically.
        for path in self.root.glob("*/workspace.json"):
            value = json.loads(path.read_text())
            if value["status"] in ACTIVE:
                value.update({"status": "failed", "message": "The controller restarted before completing this operation. No automatic model retry was started."})
                atomic_json(path, value)

    def path(self, workspace_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", workspace_id):
            raise ValueError("Invalid workspace ID.")
        return self.root / workspace_id

    def admit(self, kind: str):
        value = json.loads(self.admission_path.read_text())
        day = int(time.time()) // 86400
        if value["day"] != day:
            value = {"day": day, "uploads": 0, "recoveries": 0}
        value[kind] = value.get(kind, 0) + 1
        atomic_json(self.admission_path, value)

    def active_count(self):
        return sum(json.loads(path.read_text())["status"] in ACTIVE for path in self.root.glob("*/workspace.json"))

    def create(self, backup: bytes | None, current: bytes | None, checkpoint: bytes | None = None, *,
               format: str = "sqlite", primary_key: str = "", example: bool = False, scenario: str = "legacy",
               workflow: str = "recovery", incoming: bytes | None = None) -> dict:
        with self.lock:
            active = self.active_count()
            if active >= 4:
                raise AdmissionDenied("The four-place inspection and recovery queue is full.")
            self.admit("uploads")
            workspace_id, token = secrets.token_hex(16), secrets.token_urlsafe(32)
            path = self.path(workspace_id)
            path.mkdir(mode=0o700)
            if backup is not None:
                (path / ("backup." + format)).write_bytes(backup)
            if current is not None:
                (path / ("current." + format)).write_bytes(current)
            if checkpoint is not None:
                (path / ("checkpoint." + format)).write_bytes(checkpoint)
            if incoming is not None:
                (path / "incoming.csv").write_bytes(incoming)
            value = {"id": workspace_id, "token_sha256": sha256_bytes(token.encode()), "status": "inspecting",
                     "message": "Reading the file versions in separate temporary environments.", "example": example,
                     "format": format, "primary_key": primary_key, "scenario": scenario, "workflow": workflow,
                     "has_checkpoint": checkpoint is not None or scenario == "overlap", "model_calls": 0,
                     "created_at": int(time.time() * 1000), "events": [], "version": 1}
            atomic_json(path / "workspace.json", value)
            return {"id": workspace_id, "token": token, "status": value["status"], "workflow": workflow}

    def read(self, workspace_id: str, token: str) -> dict | None:
        try:
            path = self.path(workspace_id) / "workspace.json"
            with self.lock:
                if not path.is_file() or path.is_symlink():
                    return None
                value = json.loads(path.read_text())
                if not hmac.compare_digest(value["token_sha256"], sha256_bytes(token.encode())):
                    return None
                return {key: item for key, item in value.items() if key != "token_sha256"}
        except (OSError, ValueError):
            return None

    def update(self, workspace_id: str, **changes) -> dict:
        with self.lock:
            path = self.path(workspace_id) / "workspace.json"
            value = json.loads(path.read_text())
            value.update(changes)
            value["version"] += 1
            if "message" in changes:
                value["events"].append({"status": value["status"], "message": changes["message"], "at": int(time.time() * 1000)})
            atomic_json(path, value)
            return value

    def reserve_recovery(self, workspace_id: str, token: str, plan_sha256: str) -> dict:
        with self.lock:
            value = self.read(workspace_id, token)
            plan = (value.get("plan") or {}) if value else {}
            if (not value or value.get("workflow") == "preflight" or value["status"] != "ready" or plan.get("sha256") != plan_sha256
                    or plan.get("workspace_version") is not None and plan["workspace_version"] != value["version"]
                    or value.get("model_calls", 0) >= 2):
                raise AdmissionDenied("The selected recovery changed or is already running. Review it again.")
            if self.active_count() >= 4:
                raise AdmissionDenied("The four-place investigation and recovery queue is full.")
            self.admit("recoveries")
            self.update(workspace_id, status="queued", active_operation="recover", message="Your approved recovery is queued.")
            return value

    def reserve_diagnosis(self, workspace_id: str, token: str, instruction: str, expected_version: int) -> dict:
        with self.lock:
            value = self.read(workspace_id, token)
            if (not value or value["status"] != "ready" or not value.get("checkpoint")
                    or value["version"] != expected_version or value.get("diagnosis_started") or value.get("model_calls", 0)):
                raise AdmissionDenied("Investigation needs three checked versions and can run once per workspace. Review the current workspace.")
            if self.active_count() >= 4:
                raise AdmissionDenied("The four-place investigation and recovery queue is full.")
            self.admit("diagnoses")
            self.update(workspace_id, status="queued", active_operation="diagnose", instruction=instruction,
                        diagnosis_started=True, plan=None, message="Your investigation is queued.")
            return value

    def consume_model_call(self, workspace_id: str):
        with self.lock:
            value = json.loads((self.path(workspace_id) / "workspace.json").read_text())
            count = value.get("model_calls", 0)
            if count >= 2:
                raise AdmissionDenied("The two-call model budget has been used.")
            self.update(workspace_id, model_calls=count + 1)

    def reserve_preflight(self, workspace_id: str, token: str, plan_sha256: str) -> dict:
        with self.lock:
            value = self.read(workspace_id, token)
            plan = (value.get("import_plan") or {}) if value else {}
            if (not value or value.get("workflow") != "preflight" or value["status"] != "ready"
                    or plan.get("sha256") != plan_sha256 or plan.get("workspace_version") != value["version"]):
                raise AdmissionDenied("The import selection changed or is already running. Review it again.")
            if self.active_count() >= 4:
                raise AdmissionDenied("The four-place inspection and execution queue is full.")
            self.admit("preflights")
            self.update(workspace_id, status="queued", active_operation="preflight", message="Your approved import preview is queued.")
            return value

    def delete(self, workspace_id: str, token: str):
        with self.lock:
            value = self.read(workspace_id, token)
            if not value or value["status"] in ACTIVE:
                raise AdmissionDenied("Wait for this operation to finish before deleting its workspace.")
            path = self.path(workspace_id)
            if path.is_symlink() or not path.is_dir() or not path.resolve().is_relative_to(self.root):
                raise ValueError("The owned workspace path could not be confirmed.")
            shutil.rmtree(path)
