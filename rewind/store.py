from __future__ import annotations
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time
from .evidence import sha256_bytes


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
    def __init__(self, root: Path, *, daily_limit: int = 4):
        self.root = root.resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.daily_limit = max(1, min(daily_limit, 8))
        self.admission_path = self.root / "admission.json"
        if not self.admission_path.exists():
            atomic_json(self.admission_path, {"day": int(time.time()) // 86400, "uploads": 0, "recoveries": 0})
        # Never resume an interrupted model request automatically.
        for path in self.root.glob("*/workspace.json"):
            value = json.loads(path.read_text())
            if value["status"] in {"inspecting", "generating", "running", "checking", "finishing", "queued"}:
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
        limit = 12 if kind == "uploads" else self.daily_limit
        if value[kind] >= limit:
            raise AdmissionDenied("Today's bounded demo allowance is used up. No model request was started.")
        value[kind] += 1
        atomic_json(self.admission_path, value)

    def create(self, backup: bytes | None, current: bytes | None, *, example: bool = False) -> dict:
        with self.lock:
            active = sum(json.loads(path.read_text())["status"] in {"inspecting", "generating", "running", "checking", "finishing", "queued"}
                         for path in self.root.glob("*/workspace.json"))
            if active >= 4:
                raise AdmissionDenied("The four-place inspection and recovery queue is full.")
            if sum(1 for _ in self.root.glob("*/workspace.json")) >= 32:
                raise AdmissionDenied("The bounded demonstration storage is full.")
            self.admit("uploads")
            workspace_id, token = secrets.token_hex(16), secrets.token_urlsafe(32)
            path = self.path(workspace_id)
            path.mkdir(mode=0o700)
            if backup is not None and current is not None:
                (path / "backup.sqlite").write_bytes(backup)
                (path / "current.sqlite").write_bytes(current)
            value = {"id": workspace_id, "token_sha256": sha256_bytes(token.encode()), "status": "inspecting",
                     "message": "Reading both snapshots in separate temporary sandboxes.", "example": example,
                     "created_at": int(time.time() * 1000), "events": [], "version": 1}
            atomic_json(path / "workspace.json", value)
            return {"id": workspace_id, "token": token, "status": value["status"]}

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
            if not value or value["status"] != "ready" or value.get("plan", {}).get("sha256") != plan_sha256:
                raise AdmissionDenied("The selected recovery changed or is already running. Review it again.")
            self.admit("recoveries")
            self.update(workspace_id, status="queued", message="Your selected recovery is queued.")
            return value
