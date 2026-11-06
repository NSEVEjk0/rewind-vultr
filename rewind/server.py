from __future__ import annotations
import hmac
import json
import mimetypes
import os
from pathlib import Path
import queue
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from .contract import ScopeError, compare, compatible, make_plan
from .evidence import sha256_bytes
from .pipeline import InspectionError, decode_file, inspect_input, public_run, run_recovery
from .provider import VultrProvider
from .sandbox import Sandbox, SandboxUnavailable
from .store import AdmissionDenied, Store

WEB = Path(__file__).resolve().parent.parent / "web"
MATCH = re.compile(r"/api/workspaces/([a-f0-9]{32})(?:/(compare|plan|recover|evidence|database|backup|current))?")


class Controller:
    def __init__(self, state: Path, origin: str, access_code: str, daily_limit: int):
        self.origin = origin.rstrip("/")
        self.access_code = access_code
        self.provider = VultrProvider.from_environment()
        self.sandbox = Sandbox(state / "work")
        self.store = Store(state / "workspaces", daily_limit=daily_limit)
        self.queue = queue.Queue(maxsize=4)
        self.sandbox_ready = False
        self.readiness = "The isolated recovery worker is not ready yet."
        try:
            self.sandbox.ready()
            self.sandbox_ready = self.sandbox.collect(startup=True)["confirmed"]
            if not self.sandbox_ready:
                self.readiness = "Earlier temporary sandboxes could not be removed."
        except SandboxUnavailable as error:
            self.readiness = str(error)
        if self.sandbox_ready and not self.provider.configured:
            self.readiness = "Live recovery is unavailable while the Vultr connection is being configured."
        elif self.sandbox_ready and not self.access_code:
            self.readiness = "Live recovery is unavailable until the private demo access code is configured."
        threading.Thread(target=self.work, daemon=True, name="rewind-recovery").start()

    @property
    def available(self):
        return self.sandbox_ready and self.provider.configured and bool(self.access_code)

    @property
    def inspection_available(self):
        return self.sandbox_ready and bool(self.access_code)

    def work(self):
        while True:
            workspace_id, operation = self.queue.get()
            path = self.store.path(workspace_id)
            try:
                if operation == "inspect":
                    value = json.loads((path / "workspace.json").read_text())
                    runs = []
                    if value["example"]:
                        run = self.sandbox.run("demo")
                        if not run["ok"]:
                            raise InspectionError("The fictional example could not be created in its sandbox.", run=public_run(run))
                        runs.append(public_run(run))
                        for name in ("backup", "current"):
                            (path / (name + ".sqlite")).write_bytes(decode_file(run["payload"][name]))
                    backup, backup_run = inspect_input((path / "backup.sqlite").read_bytes(), self.sandbox)
                    runs.append(backup_run)
                    self.store.update(workspace_id, inspection_runs=runs)
                    current, current_run = inspect_input((path / "current.sqlite").read_bytes(), self.sandbox)
                    runs.append(current_run)
                    compatible(backup, current)
                    self.store.update(workspace_id, status="ready", message="Both snapshots were read and checked. Choose the damaged cells to restore.",
                                      backup=backup, current=current, inspection_runs=runs)
                else:
                    value = json.loads((path / "workspace.json").read_text())
                    evidence, artifact = run_recovery(backup=value["backup"], current=value["current"], plan=value["plan"],
                        backup_raw=(path / "backup.sqlite").read_bytes(), current_raw=(path / "current.sqlite").read_bytes(),
                        prior_runs=value["inspection_runs"], sandbox=self.sandbox, provider=self.provider,
                        progress=lambda stage, message: self.store.update(workspace_id, status=stage, message=message))
                    if artifact is not None:
                        (path / "verified.sqlite").write_bytes(artifact)
                    payload = evidence["payload"]
                    if not payload["all_sandboxes_removed"]:
                        self.sandbox_ready = False
                        self.readiness = "Temporary sandbox removal could not be confirmed."
                    self.store.update(workspace_id, status=payload["status"], message=payload["message"], evidence=evidence)
            except (InspectionError, ScopeError) as error:
                failed_run = getattr(error, "run", None)
                changes = {"status": "failed", "message": str(error)[:350]}
                if failed_run:
                    changes["failed_inspection_run"] = failed_run
                    if not failed_run.get("teardown", {}).get("confirmed"):
                        self.sandbox_ready = False
                        self.readiness = "Temporary sandbox removal could not be confirmed."
                        changes["message"] = self.readiness + " No further recovery is available until it is resolved."
                self.store.update(workspace_id, **changes)
            except Exception:
                self.store.update(workspace_id, status="failed", message="The controller could not complete this operation. No additional model request was started.")
            finally:
                self.queue.task_done()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, controller):
        self.controller = controller
        self.slots = threading.BoundedSemaphore(24)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(12)

    def log_message(self, format, *args):
        # No access codes, workspace capabilities, customer rows or URLs in logs.
        pass

    def send(self, status, body, content_type="application/json; charset=utf-8", filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        if filename:
            self.send_header("Content-Disposition", 'attachment; filename="' + filename + '"')
        self.end_headers()
        self.wfile.write(body)

    def json(self, status, value):
        self.send(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode())

    def token(self):
        header = self.headers.get("Authorization", "")
        return header[7:] if header.startswith("Bearer ") else ""

    def workspace(self, workspace_id):
        return self.server.controller.store.read(workspace_id, self.token())

    def do_GET(self):
        path = urlsplit(self.path).path
        controller = self.server.controller
        if path == "/api/health":
            self.json(200, {"ok": True, "sandbox_ready": controller.sandbox_ready, "inference_configured": controller.provider.configured})
            return
        if path == "/api/config":
            self.json(200, {"available": controller.available, "inspection_available": controller.inspection_available,
                            "message": "The Vultr recovery worker is connected." if controller.available else controller.readiness,
                            "model": controller.provider.model, "access_required": True, "daily_limit": controller.store.daily_limit,
                            "max_file_bytes": 256 * 1024, "max_rows": 1000, "demo_available": (WEB / "demo/index.html").is_file(),
                            "data_sent": "Only table/column names and types, selected columns and record count are sent to Vultr AI."})
            return
        match = MATCH.fullmatch(path)
        if match:
            workspace_id, operation = match.groups()
            value = self.workspace(workspace_id)
            if not value:
                self.json(404, {"error": "Workspace not found or access token missing."})
                return
            if operation == "evidence":
                if not value.get("evidence"):
                    self.json(409, {"error": "There is no completed recovery evidence yet."})
                else:
                    self.json(200, value["evidence"])
            elif operation in {"database", "backup", "current"}:
                if operation == "database":
                    evidence = value.get("evidence", {}).get("payload", {})
                    if value["status"] != "verified" or not evidence.get("all_sandboxes_removed"):
                        self.json(409, {"error": "A recovery can be downloaded only after every check and cleanup passes."})
                        return
                    filename = "verified.sqlite"
                else:
                    filename = operation + ".sqlite"
                target = controller.store.path(workspace_id) / filename
                if not target.is_file() or target.is_symlink():
                    self.json(409, {"error": "The database copy is not ready."})
                    return
                raw = target.read_bytes()
                expected = (evidence.get("artifact_sha256") if operation == "database" else value.get(operation, {}).get("file_sha256"))
                if sha256_bytes(raw) != expected:
                    self.json(409, {"error": "The file differs from the checked fingerprint."})
                    return
                self.send(200, raw, "application/vnd.sqlite3", "rewind-recovered.sqlite" if operation == "database" else "rewind-" + operation + ".sqlite")
            elif operation is None:
                self.json(200, value)
            else:
                self.json(405, {"error": "Use a POST request for this action."})
            return
        relative = "index.html" if path == "/" else path.lstrip("/")
        target = (WEB / relative).resolve()
        if not target.is_relative_to(WEB) or not target.is_file() or target.suffix not in {".html", ".css", ".js", ".svg", ".mp4", ".vtt", ".pdf", ".json"}:
            self.json(404, {"error": "Page not found."})
            return
        self.send(200, target.read_bytes(), mimetypes.guess_type(target)[0] or "application/octet-stream")

    def do_POST(self):
        controller = self.server.controller
        path = urlsplit(self.path).path
        if self.headers.get("Origin", "").rstrip("/") != controller.origin:
            self.json(403, {"error": "Use the configured Rewind site."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 750000 or not self.headers.get("Content-Type", "").startswith("application/json"):
                raise ValueError("Send a bounded JSON request.")
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict):
                raise ValueError("Send a JSON object.")
            if path in {"/api/workspaces", "/api/example"}:
                if not self.has_access(require_inference=False):
                    return
                if path == "/api/example":
                    if request:
                        raise ValueError("The example action takes no fields.")
                    admission = controller.store.create(None, None, example=True)
                else:
                    if set(request) != {"backup", "current"}:
                        raise ValueError("Provide a good backup and today's consistent snapshot.")
                    admission = controller.store.create(decode_file(request["backup"]), decode_file(request["current"]))
                try:
                    controller.queue.put_nowait((admission["id"], "inspect"))
                except queue.Full:
                    controller.store.update(admission["id"], status="failed", message="The bounded queue is full.")
                    raise AdmissionDenied("The bounded queue is full.")
                self.json(202, admission)
                return
            match = MATCH.fullmatch(path)
            if not match:
                self.json(404, {"error": "Action not found."})
                return
            workspace_id, operation = match.groups()
            value = self.workspace(workspace_id)
            if not value:
                self.json(404, {"error": "Workspace not found or access token missing."})
                return
            if operation == "recover":
                if not self.has_access() or not controller.available:
                    return
                if set(request) != {"plan_sha256"}:
                    raise ValueError("Approve the currently displayed recovery plan.")
                controller.store.reserve_recovery(workspace_id, self.token(), request["plan_sha256"])
                try:
                    controller.queue.put_nowait((workspace_id, "recover"))
                except queue.Full:
                    controller.store.update(workspace_id, status="failed", message="The bounded queue is full; no model request was started.")
                    raise AdmissionDenied("The bounded queue is full.")
                self.json(202, {"status": "queued"})
                return
            if operation not in {"compare", "plan"} or value["status"] != "ready":
                self.json(409, {"error": "Wait for both snapshots to be checked before choosing a recovery."})
                return
            if operation == "compare":
                if set(request) != {"columns"}:
                    raise ValueError("Choose the columns to compare.")
                self.json(200, compare(value["backup"], value["current"], request["columns"]))
            else:
                if set(request) != {"columns", "keys", "expected_version"}:
                    raise ValueError("Choose columns and records from the current workspace.")
                with controller.store.lock:
                    value = self.workspace(workspace_id)
                    if value["status"] != "ready" or value["version"] != request["expected_version"]:
                        self.json(409, {"error": "The workspace changed. Review the current selection."})
                        return
                    plan = make_plan(value["backup"], value["current"], request["columns"], request["keys"])
                    controller.store.update(workspace_id, plan=plan)
                self.json(200, self.workspace(workspace_id))
        except AdmissionDenied as error:
            self.json(429, {"error": str(error)})
        except (ScopeError, InspectionError) as error:
            self.json(400, {"error": str(error)[:350]})
        except (ValueError, TypeError, KeyError):
            self.json(400, {"error": "Choose valid snapshots and a bounded recovery selection."})

    def has_access(self, *, require_inference=True):
        controller = self.server.controller
        if not (controller.available if require_inference else controller.inspection_available):
            self.json(503, {"error": controller.readiness})
            return False
        if not hmac.compare_digest(self.headers.get("X-Rewind-Access", ""), controller.access_code):
            self.json(403, {"error": "Enter the private demo access code to use the recovery worker."})
            return False
        return True


def main():
    os.umask(0o077)
    state = Path(os.environ.get("REWIND_STATE_DIR", ".local/state"))
    controller = Controller(state, os.environ.get("REWIND_ORIGIN", "http://127.0.0.1:18182"),
                            os.environ.get("REWIND_ACCESS_CODE", ""), int(os.environ.get("REWIND_DAILY_RECOVERIES", "4")))
    server = Server((os.environ.get("REWIND_HOST", "127.0.0.1"), int(os.environ.get("REWIND_PORT", "18182"))), controller)
    server.serve_forever()


if __name__ == "__main__":
    main()
