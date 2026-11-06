"""Bounded process capture adapted from ReproLab (MIT).

Generated code never runs in the controller process. All process output is
streamed into a shared byte budget, including Docker control commands.
"""
from __future__ import annotations
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from .evidence import canonical, sha256_bytes

IMAGE = "python:3.12-slim-bookworm@sha256:623674a14ef93d052bb9b0de4dbf5f28eb578a35263838941cc8aa43213c1487"
MEMORY_BYTES = 192 * 1024 * 1024
OUTPUT_LIMIT = 384 * 1024
SANDBOX_SECONDS = 8.0


@dataclass(frozen=True)
class Captured:
    stdout: bytes
    stderr: bytes
    exit_code: int | None
    stop_reason: str | None
    elapsed_ms: int


def capture(command: list[str], *, input_bytes: bytes = b"", seconds: float = 8.0,
            output_limit: int = OUTPUT_LIMIT) -> Captured:
    """No shell, no inherited credentials, bounded input/output and wall time."""
    if seconds <= 0 or output_limit <= 0 or len(input_bytes) > 128 * 1024:
        raise ValueError("Invalid process limits")
    start = time.monotonic()
    deadline = start + seconds
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    used = 0
    stop_reason = None
    try:
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True,
                                cwd="/", env={"PATH": os.defpath, "LANG": "C.UTF-8"})
    except OSError as error:
        return Captured(b"", str(error).encode()[:500], None, "spawn_error", 0)
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    remaining_input = memoryview(input_bytes)
    try:
        with selectors.DefaultSelector() as selector:
            for pipe, name in [(proc.stdout, "stdout"), (proc.stderr, "stderr")]:
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            if input_bytes:
                os.set_blocking(proc.stdin.fileno(), False)
                selector.register(proc.stdin, selectors.EVENT_WRITE, "stdin")
            else:
                proc.stdin.close()
            while selector.get_map():
                available = deadline - time.monotonic()
                if available <= 0:
                    stop_reason = "timeout"
                    break
                for key, _ in selector.select(min(available, 0.1)):
                    pipe, name = key.fileobj, key.data
                    if name == "stdin":
                        try:
                            written = os.write(pipe.fileno(), remaining_input[:8192])
                            remaining_input = remaining_input[written:]
                        except BlockingIOError:
                            continue
                        except (BrokenPipeError, OSError):
                            remaining_input = memoryview(b"")
                        if not remaining_input:
                            selector.unregister(pipe)
                            pipe.close()
                        continue
                    try:
                        chunk = os.read(pipe.fileno(), min(8192, output_limit - used + 1))
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(pipe)
                        pipe.close()
                        continue
                    accepted = chunk[:max(0, output_limit - used)]
                    buffers[name].extend(accepted)
                    used += len(accepted)
                    if len(chunk) > len(accepted):
                        stop_reason = "output_limit"
                        break
                if stop_reason:
                    break
            if not stop_reason:
                try:
                    proc.wait(timeout=max(0.001, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    stop_reason = "timeout"
    finally:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=3)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if not pipe.closed:
                pipe.close()
    return Captured(bytes(buffers["stdout"]), bytes(buffers["stderr"]), proc.returncode,
                    stop_reason, round((time.monotonic() - start) * 1000))


class SandboxUnavailable(RuntimeError):
    pass


class Sandbox:
    def __init__(self, work_root: Path, *, docker: str = "/usr/bin/docker"):
        self.image, self.docker = IMAGE, docker
        self.work_root = work_root.resolve()
        self.work_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.instance = sha256_bytes(str(self.work_root).encode())[:24]

    def control(self, *arguments: str) -> Captured:
        return capture([self.docker, "--host=unix:///var/run/docker.sock", *arguments],
                       seconds=5, output_limit=64 * 1024)

    def ready(self) -> dict:
        # Verify the named runtime is the pinned binary and complete bundle.
        # Merely finding a runtime named "runsc" would not establish gVisor.
        from .runtime import matching_runtime, pinned_bundle
        runtimes = self.control("info", "--format", "{{json .Runtimes}}")
        if runtimes.exit_code != 0 or runtimes.stop_reason:
            raise SandboxUnavailable("The controller cannot reach the Docker daemon.")
        try:
            if not matching_runtime(json.loads(runtimes.stdout).get("runsc")):
                raise SandboxUnavailable("The pinned gVisor runtime is required.")
        except (ValueError, TypeError) as error:
            raise SandboxUnavailable("The Docker runtime list could not be verified.") from error
        try:
            pinned_bundle()
        except (OSError, RuntimeError) as error:
            raise SandboxUnavailable("The complete pinned gVisor bundle could not be verified.") from error
        image = self.control("image", "inspect", self.image, "--format", "{{.Id}}")
        if image.exit_code != 0 or image.stop_reason:
            raise SandboxUnavailable("The pinned Python sandbox image is required.")
        return {"runtime": "runsc", "image_ref": self.image, "image_id": image.stdout.decode().strip()}

    def command(self, name: str, work: Path) -> list[str]:
        if not re.fullmatch(r"rewind-[a-f0-9]{32}", name) or not work.is_relative_to(self.work_root):
            raise ValueError("Invalid owned sandbox.")
        return [self.docker, "--host=unix:///var/run/docker.sock", "run", "--pull=never", "--runtime=runsc",
                "--name", name, "--label", "io.rewind.owned=true", "--label", "io.rewind.instance=" + self.instance,
                "--label", "io.rewind.expires=" + str(int(time.time()) + 30),
                "--network=none", "--read-only", "--cpus=0.5", "--memory=192m", "--memory-swap=192m",
                "--pids-limit=48", "--ulimit=nofile=64:64", "--tmpfs=/tmp:rw,noexec,nosuid,size=16m",
                "--user=65532:65532", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--log-driver=none", "--mount", "type=bind,source=" + str(work) + ",target=/work,readonly",
                "-i", self.image, "python", "-I", "-B", "/work/worker.py"]

    @staticmethod
    def policy(value: dict) -> dict[str, bool]:
        host, config = value.get("HostConfig", {}), value.get("Config", {})
        mounts = value.get("Mounts", [])
        return {
            "gvisor": host.get("Runtime") == "runsc",
            "pinned_image": config.get("Image") == IMAGE,
            "no_network": host.get("NetworkMode") == "none",
            "readonly_root": host.get("ReadonlyRootfs") is True,
            "nonroot": config.get("User") == "65532:65532",
            "memory_limit": host.get("Memory") == MEMORY_BYTES and host.get("MemorySwap") == MEMORY_BYTES,
            "cpu_limit": host.get("NanoCpus") == 500_000_000,
            "pid_limit": host.get("PidsLimit") == 48,
            "no_privilege": host.get("Privileged") is False and "ALL" in host.get("CapDrop", [])
                            and any(item.startswith("no-new-privileges") for item in host.get("SecurityOpt", [])),
            "readonly_inputs": len(mounts) == 1 and mounts[0].get("Destination") == "/work" and mounts[0].get("RW") is False,
            "bounded_tmp": host.get("Tmpfs", {}).get("/tmp", "").startswith("rw,noexec,nosuid,size=16m"),
            "no_docker_logs": host.get("LogConfig", {}).get("Type") == "none",
        }

    def teardown(self, name: str) -> dict:
        if not re.fullmatch(r"rewind-[a-f0-9]{32}", name):
            raise ValueError("Invalid owned sandbox.")
        removed = self.control("rm", "--force", name)
        absent = self.control("ps", "--all", "--filter", "name=^" + name + "$", "--format", "{{.ID}}")
        return {"confirmed": absent.exit_code == 0 and absent.stop_reason is None and not absent.stdout.strip(),
                "remove_exit_code": removed.exit_code, "absence_query_exit_code": absent.exit_code}

    def collect(self, *, startup: bool = False) -> dict:
        listed = self.control("ps", "--all", "--filter", "label=io.rewind.owned=true", "--filter",
                              "label=io.rewind.instance=" + self.instance, "--format", "{{.Names}}")
        if listed.exit_code != 0 or listed.stop_reason:
            return {"confirmed": False, "removed": []}
        removed = []
        for name in listed.stdout.decode().splitlines():
            if not re.fullmatch(r"rewind-[a-f0-9]{32}", name):
                continue
            response = self.control("inspect", name, "--format", "{{json .Config.Labels}}")
            try:
                labels = json.loads(response.stdout)
                owned = labels.get("io.rewind.owned") == "true" and labels.get("io.rewind.instance") == self.instance
                expired = int(labels.get("io.rewind.expires", "0")) < int(time.time())
            except (ValueError, TypeError):
                continue
            if response.exit_code == 0 and not response.stop_reason and owned and (startup or expired):
                removed.append({"sandbox_id": name, "teardown": self.teardown(name)})
        return {"confirmed": all(item["teardown"]["confirmed"] for item in removed), "removed": removed}

    def run(self, operation: str, files: dict[str, bytes] | None = None, *, scope: dict | None = None,
            seconds: float = SANDBOX_SECONDS) -> dict[str, Any]:
        files = files or {}
        if operation not in {"inspect", "candidate", "demo"} or not 1 <= seconds <= 15:
            raise ValueError("Unsupported bounded sandbox operation.")
        if set(files) - {"backup.sqlite", "current.sqlite", "repair.py"} or sum(len(raw) for raw in files.values()) > 600 * 1024:
            raise ValueError("Invalid sandbox input files.")
        if any(not isinstance(raw, bytes) or len(raw) > (24 * 1024 if name == "repair.py" else 256 * 1024) for name, raw in files.items()):
            raise ValueError("Sandbox input exceeds its byte limit.")
        nonce, name = uuid.uuid4().hex, "rewind-" + uuid.uuid4().hex
        work = Path(tempfile.mkdtemp(prefix="run-", dir=self.work_root))
        work.chmod(0o755)
        worker = Path(__file__).with_name("worker.py").read_bytes()
        result = {"operation": operation, "sandbox_id": name, "nonce": nonce, "image_ref": self.image,
                  "worker_sha256": sha256_bytes(worker), "input_hashes": {key: sha256_bytes(raw) for key, raw in files.items()},
                  "policy": {}, "policy_verified": False, "ok": False, "started_at": int(time.time() * 1000)}
        try:
            for filename, raw in {"worker.py": worker, **files}.items():
                path = work / filename
                path.write_bytes(raw)
                path.chmod(0o644)
            request = {"nonce": nonce, "operation": operation, "scope": scope or {}}
            outcome = capture(self.command(name, work), input_bytes=canonical(request), seconds=seconds, output_limit=OUTPUT_LIMIT)
            result.update({"exit_code": outcome.exit_code, "stop_reason": outcome.stop_reason,
                           "elapsed_ms": outcome.elapsed_ms, "stdout_bytes": len(outcome.stdout), "stderr_bytes": len(outcome.stderr),
                           "stdout_sha256": sha256_bytes(outcome.stdout)})
            inspection = self.control("inspect", name, "--format", "{{json .}}")
            if inspection.exit_code == 0 and not inspection.stop_reason:
                try:
                    value = json.loads(inspection.stdout)
                    policy = self.policy(value)
                    result.update({"runtime": value["HostConfig"].get("Runtime"), "image_id": value.get("Image"),
                                   "policy": policy, "policy_verified": all(policy.values()),
                                   "oom_killed": value.get("State", {}).get("OOMKilled", False)})
                except (ValueError, TypeError, KeyError):
                    pass
            if outcome.exit_code == 0 and not outcome.stop_reason:
                try:
                    response = json.loads(outcome.stdout)
                    if response.get("nonce") == nonce and response.get("ok") is True:
                        result.update({"ok": True, "payload": response["payload"]})
                    else:
                        result["error"] = str(response.get("error", "Invalid worker output."))[:300]
                except (ValueError, TypeError, KeyError):
                    result["error"] = "The sandbox returned an invalid output protocol."
            else:
                result["error"] = "The sandbox exceeded its limits or did not finish."
        finally:
            result["teardown"] = self.teardown(name)
            result["finished_at"] = int(time.time() * 1000)
            shutil.rmtree(work)
        result["ok"] = result["ok"] and result["policy_verified"] and result["teardown"]["confirmed"]
        return result
