"""Register pinned gVisor by Docker reload and verify existing containers.

Docker may reorder mounts and the runtime's supported-capability report.
Normalize those sets while comparing every entry, captured container value,
runtime path/argument and other reported feature.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import time
from .evidence import canonical, sha256_bytes
from .sandbox import capture

ROOT = Path("/opt/reprolab-runtime/release-20261102.0")
RUNTIME = ROOT / "runsc"
PINS = {
    "containerd-shim-runsc-v1": "6187d6d34f4996e859353fbb4c56d5ed41fc35b5afa44bff766f4e4b424da5bb",
    "gvisor-bin/checkpointgofer": "37e6e92d6dcb3296e10dc2f21b0ef6156f1373ff0668d27a4fccc8ded2b51f94",
    "gvisor-bin/gvisor-proc-umounter": "b3bbbaef5ddc62084e5febd19d3acca14f3d42b2a25d8552fb40def699467019",
    "gvisor-bin/gvisor-sentry-prewarmer": "046e3ec1de3aa7b7ec7ce53b4f0d1e08db15987847606a8e361133d5c5bd3d73",
    "gvisor-bin/gvisor_sentry": "6dfd3af65e664663f332d0e4b9fb82452496e34ebc6cb568ae49f5b1ac9d8166",
    "gvisor-bin/runsc-fd-parking": "03af90d07ea466c10ecc3e3ba839b2a59540959dd3286aea3886917cba850c11",
    "gvisor-bin/runsc-metric-server": "b9857ebe290c403fda7ff014368df3a0f45e5428dc1eb6efe413cfe0e5a940f4",
    "runsc": "c5a5a7ba1209b605d08874fdd4429035f95afcb2d482bc95ed3fb13d31a25098",
}
CONFIG = Path("/etc/docker/daemon.json")
INFO = ('{"id":{{json .ID}},"version":{{json .ServerVersion}},'
        '"root":{{json .DockerRootDir}},"default_runtime":{{json .DefaultRuntime}},'
        '"runtimes":{{json .Runtimes}}}')
CONTAINER = ('{"id":{{json .Id}},"name":{{json .Name}},"image":{{json .Image}},'
             '"running":{{json .State.Running}},"paused":{{json .State.Paused}},'
             '"status":{{json .State.Status}},"started_at":{{json .State.StartedAt}},'
             '"restart_count":{{json .RestartCount}},"runtime":{{json .HostConfig.Runtime}},'
             '"privileged":{{json .HostConfig.Privileged}},"caps":{{json .HostConfig.CapAdd}},'
             '"pid_mode":{{json .HostConfig.PidMode}},"mounts":{{json .Mounts}}}')


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def pinned_bundle():
    for relative, expected in PINS.items():
        path = ROOT / relative
        require(path.is_file() and not any(part.is_symlink() for part in [path, *path.parents]),
                "The complete pinned gVisor bundle is required.")
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        require(actual == expected, "A pinned gVisor binary does not match its checksum.")
    return {"runtime_path": str(RUNTIME), "bundle_verified": True, "file_sha256": PINS}


def command(args, *, seconds=12):
    result = capture(args, seconds=seconds, output_limit=1024 * 1024)
    require(result.exit_code == 0 and result.stop_reason is None,
            "Host command failed for " + Path(args[0]).name + "; private output was withheld.")
    return result.stdout.decode().strip()


def docker(*args):
    return command(["/usr/bin/docker", "--host=unix:///var/run/docker.sock", *args])


def snapshot():
    ids = docker("ps", "--all", "--no-trunc", "--format", "{{.ID}}").splitlines()
    return {"daemon": json.loads(docker("info", "--format", INFO)),
            "containers": {value: json.loads(docker("inspect", value, "--format", CONTAINER)) for value in ids}}


def normalized_containers(values):
    result = copy.deepcopy(values)
    for item in result.values():
        item["mounts"] = sorted(item.get("mounts") or [], key=canonical)
    return result


def normalized_runtime(value):
    result = copy.deepcopy(value)
    features_key = "org.opencontainers.runtime-spec.features"
    raw = result.get("status", {}).get(features_key)
    if isinstance(raw, str):
        features = json.loads(raw)
        capabilities = features.get("linux", {}).get("capabilities")
        if isinstance(capabilities, list) and all(isinstance(item, str) for item in capabilities):
            features["linux"]["capabilities"] = sorted(capabilities)
        result["status"][features_key] = canonical(features).decode()
    return result


def preserved(before, after):
    for key in ("id", "version", "root", "default_runtime"):
        require(before["daemon"][key] == after["daemon"][key], "Docker installation or default runtime changed.")
    for name, value in before["daemon"]["runtimes"].items():
        observed = after["daemon"]["runtimes"].get(name)
        require(observed is not None and normalized_runtime(observed) == normalized_runtime(value),
                "An existing Docker runtime changed.")
    require(normalized_containers(before["containers"]) == normalized_containers(after["containers"]),
            "An existing container changed identity, state, restart count, image, policy or mount contents.")


def matching_runtime(entry):
    return isinstance(entry, dict) and entry.get("path") == str(RUNTIME) and not entry.get("runtimeArgs")


def merged_config(existing, live):
    require(isinstance(existing, dict), "Docker configuration must be an object.")
    result = copy.deepcopy(existing)
    runtimes = result.setdefault("runtimes", {})
    require(isinstance(runtimes, dict), "Docker runtimes configuration is invalid.")
    if "runsc" in runtimes:
        entry = dict(runtimes["runsc"])
        if not entry.get("runtimeArgs"):
            entry.pop("runtimeArgs", None)
        require(entry == {"path": str(RUNTIME)}, "A different runsc configuration will not be overwritten.")
    if "runsc" in live:
        require(matching_runtime(live["runsc"]), "A different live runsc runtime will not be overwritten.")
    runtimes.setdefault("runsc", {"path": str(RUNTIME)})
    return result


def check_config_path():
    require(not any(path.is_symlink() for path in [CONFIG, *CONFIG.parents]), "Docker config path contains a symlink.")
    require(not CONFIG.exists() or CONFIG.is_file(), "Docker config is not a regular file.")


def atomic_write(path, raw, metadata=None, *, private=False):
    path.parent.mkdir(mode=0o700 if private else 0o755, parents=True, exist_ok=True)
    require(not path.is_symlink() and not path.parent.is_symlink(), "Managed path contains a symlink.")
    fd, temp = tempfile.mkstemp(prefix=".rewind-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
            os.fchmod(output.fileno(), stat.S_IMODE(metadata.st_mode) if metadata else (0o600 if private else 0o644))
            if metadata:
                os.fchown(output.fileno(), metadata.st_uid, metadata.st_gid)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def record(path, value):
    atomic_write(path, canonical(value) + b"\n", private=True)


def enable():
    require(os.geteuid() == 0, "Runtime registration requires root.")
    pinned_bundle()
    require(command(["systemctl", "is-active", "docker.service"]) == "active", "Docker must already be running.")
    require(command(["systemctl", "show", "docker.service", "--property=CanReload", "--value"]) == "yes",
            "Docker must support reload; no restart is attempted.")
    pid = command(["systemctl", "show", "docker.service", "--property=MainPID", "--value"])
    require(pid.isdecimal() and int(pid) > 0, "Docker PID is unavailable.")
    args = Path("/proc", pid, "cmdline").read_bytes().decode().split("\0")
    require(Path(args[0]).name == "dockerd", "Unsupported Docker service wrapper.")
    for index, argument in enumerate(args):
        require(not argument.startswith("--add-runtime"), "Docker runtime flags conflict with file registration.")
        if argument == "--config-file":
            require(args[index + 1] == str(CONFIG), "A custom Docker configuration path is in use.")
        elif argument.startswith("--config-file="):
            require(argument.split("=", 1)[1] == str(CONFIG), "A custom Docker configuration path is in use.")
    check_config_path()
    before = snapshot()
    original = CONFIG.read_bytes() if CONFIG.exists() else None
    metadata = CONFIG.stat() if original is not None else None
    existing = json.loads(original) if original is not None else {}
    proposed = merged_config(existing, before["daemon"]["runtimes"])
    if proposed == existing and matching_runtime(before["daemon"]["runtimes"].get("runsc")):
        print("Pinned gVisor is already configured; no Docker reload was needed.")
        return
    raw = json.dumps(proposed, indent=2).encode() + b"\n"
    daemon = shutil.which("dockerd")
    require(daemon is not None, "Docker configuration validation is unavailable.")
    with tempfile.TemporaryDirectory(prefix="rewind-runtime-") as directory:
        candidate = Path(directory) / "daemon.json"
        candidate.write_bytes(raw)
        candidate.chmod(0o600)
        command([daemon, "--validate", "--config-file", str(candidate)])
    require((CONFIG.read_bytes() if CONFIG.exists() else None) == original, "Docker config changed during preparation.")
    path = Path("/etc/rewind/runtime-change.json")
    backup = path.parent / ("docker-before-" + str(time.time_ns()) + ".json")
    atomic_write(backup, original if original is not None else b"{}\n", private=True)
    details = {"original_existed": original is not None, "original_sha256": sha256_bytes(original) if original is not None else None,
               "backup": str(backup), "runtime_path": str(RUNTIME), "state": "applying"}
    record(path, details)
    atomic_write(CONFIG, raw, metadata)
    try:
        command(["systemctl", "reload", "docker.service"])
        for attempt in range(10):
            after = snapshot()
            preserved(before, after)
            if matching_runtime(after["daemon"]["runtimes"].get("runsc")):
                break
            time.sleep(1)
        require(matching_runtime(after["daemon"]["runtimes"].get("runsc")), "Pinned runtime activation was not confirmed.")
    except Exception:
        require(CONFIG.read_bytes() == raw, "Concurrent Docker config edit detected; private backup is available.")
        if original is None:
            CONFIG.unlink()
        else:
            atomic_write(CONFIG, original, metadata)
        command(["systemctl", "reload", "docker.service"])
        preserved(before, snapshot())
        record(path, {**details, "state": "rolled-back"})
        raise RuntimeError("Runtime activation failed; original configuration was restored without a Docker restart.") from None
    record(path, {**details, "state": "complete", "other_containers_preserved": True,
                  "other_container_count": len(before["containers"]), "default_runtime": after["daemon"]["default_runtime"]})
    print("Pinned gVisor registered by reload; existing Docker defaults and all containers were preserved.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["verify-bundle", "enable"])
    args = parser.parse_args()
    try:
        if args.command == "enable":
            enable()
        else:
            pinned_bundle()
            print("Complete pinned gVisor bundle verified.")
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(1, "Rewind runtime setup stopped: " + str(error) + "\n")
