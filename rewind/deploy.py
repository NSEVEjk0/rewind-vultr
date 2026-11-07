"""Install a reviewed Rewind release beside an existing HTTPS application."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shlex
import shutil
import sys
import tempfile
import urllib.request
import time
from .evidence import canonical, sha256_bytes
from .runtime import atomic_write, command, require
from .sandbox import capture

SOURCE = Path(__file__).resolve().parent.parent
MANAGED_FILES = {"rewind.service", "rewind-gc.service", "rewind-gc.timer"}
ACTIVE = {"inspecting", "generating", "running", "checking", "finishing", "queued"}


def hostname(value):
    require(isinstance(value, str) and 1 <= len(value) <= 253 and "." in value,
            "Use a full public hostname.")
    require(all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part) for part in value.split(".")),
            "The hostname is invalid.")
    return value.lower()


def environment(raw):
    result = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        require("=" in line, "The private environment file contains an unsupported line.")
        name, value = line.split("=", 1)
        require(re.fullmatch(r"[A-Z_][A-Z0-9_]*", name.strip()) is not None,
                "The private environment contains an invalid setting name.")
        parts = shlex.split(value, comments=True, posix=True)
        require(len(parts) <= 1, "Quote values containing spaces in the private environment.")
        result[name.strip()] = parts[0] if parts else ""
    return result


def private_environment(path):
    require(path.is_file() and not path.is_symlink() and path.stat().st_uid == 0,
            "Use an existing root-owned private provider environment file.")
    require(not path.stat().st_mode & 0o007 and path.stat().st_size <= 64 * 1024,
            "The provider environment must not be world-readable or oversized.")
    return environment(path.read_text())


def proxy_plan(original, host):
    host = hostname(host)
    begin = "# BEGIN Rewind route: " + host + "\n"
    end = "# END Rewind route: " + host + "\n"
    block = (begin + host + " {\n"
             "    reverse_proxy 127.0.0.1:18182\n"
             "    header Referrer-Policy no-referrer\n"
             "    header X-Content-Type-Options nosniff\n"
             "}\n" + end).encode()
    if block in original:
        require(original.count(block) == 1, "Duplicate Rewind routes need operator review.")
        return original
    require(host.encode() not in original and b"# BEGIN Rewind route:" not in original,
            "An existing route conflicts with the requested Rewind hostname.")
    return original + (b"\n" if not original.endswith(b"\n") else b"") + b"\n" + block


def verify_source(root):
    path = root / "release-manifest.json"
    require(path.is_file() and not path.is_symlink(), "Build and unpack a release with its manifest before installing.")
    manifest = json.loads(path.read_text())
    require(manifest.get("format") == 1 and isinstance(manifest.get("files"), dict), "Unsupported release manifest.")
    digests = {}
    for relative, metadata in manifest["files"].items():
        file = root / relative
        require(not Path(relative).is_absolute() and ".." not in Path(relative).parts
                and not any(part.startswith(".local") for part in Path(relative).parts), "Invalid release path.")
        require(file.is_file() and not any(part.is_symlink() for part in [file, *file.parents] if part.is_relative_to(root)),
                "A release file is missing or linked.")
        raw = file.read_bytes()
        require(len(raw) == metadata["bytes"] and sha256_bytes(raw) == metadata["sha256"], "A release file differs from its reviewed manifest.")
        digests[relative] = metadata["sha256"]
    require(sha256_bytes(canonical(digests)) == manifest["source_sha256"], "Release source fingerprint differs.")
    return manifest


def check_managed(path, marker):
    require(not path.is_symlink(), "A Rewind managed file is linked to another location.")
    if path.exists():
        require(path.is_file() and path.read_text().startswith(marker), "A Rewind path contains an unrelated file.")


def active_jobs(state):
    return any(json.loads(path.read_text()).get("status") in ACTIVE for path in (state / "workspaces").glob("*/workspace.json"))


def install(args):
    require(os.geteuid() == 0, "Run the release installer as root on the Vultr VM.")
    require(sys.version_info >= (3, 12), "Python 3.12 or newer is required.")
    manifest = verify_source(SOURCE)
    host = hostname(args.hostname)
    require(args.caddy_config in {Path("/etc/caddy/reprolab.Caddyfile"), Path("/etc/caddy/Caddyfile")},
            "Supply the inspected existing Caddy configuration path.")
    require(args.caddy_service in {"reprolab-https.service", "caddy.service"}, "Supply the inspected existing Caddy service.")
    config = args.caddy_config
    require(config.is_file() and not config.is_symlink() and not config.parent.is_symlink(), "Existing Caddy config must be a regular file.")
    require(command(["systemctl", "is-active", args.caddy_service]) == "active", "Existing HTTPS service must already be active.")
    require(command(["systemctl", "show", args.caddy_service, "--property=CanReload", "--value"]) == "yes",
            "The HTTPS service must support reload; it will not be restarted.")
    original_proxy = config.read_bytes()
    proposed_proxy = proxy_plan(original_proxy, host)
    proxy_metadata = config.stat()
    caddy = shutil.which("caddy")
    require(caddy is not None, "The existing Caddy binary is required.")
    with tempfile.NamedTemporaryFile(prefix=".rewind-validate-", dir=config.parent) as file:
        file.write(proposed_proxy)
        file.flush()
        command([caddy, "validate", "--adapter", "caddyfile", "--config", file.name])
    state = Path("/var/lib/rewind")
    require(not active_jobs(state), "A Rewind job is active; install again after it completes.")
    releases = Path("/opt/rewind/releases")
    link = releases.parent / "current"
    require(not state.is_symlink() and not releases.parent.is_symlink() and not releases.is_symlink(),
            "Rewind directories must not be symbolic links.")
    require(not state.exists() or state.is_dir(), "The Rewind state path contains an unrelated file.")
    if link.exists() or link.is_symlink():
        require(link.is_symlink() and link.resolve().is_relative_to(releases) and link.resolve().is_dir(),
                "The current release link points outside Rewind's release directory.")
        verify_source(link.resolve())
    for name in MANAGED_FILES:
        check_managed(Path("/etc/systemd/system") / name, "# Rewind managed")
    provider = private_environment(args.provider_env)
    key = provider.get("VULTR_INFERENCE_API_KEY", "")
    model = provider.get("VULTR_INFERENCE_MODEL", "glm-5.3-flash")
    require(8 <= len(key) <= 2048 and not any(c in key for c in "\n\r\0"), "The existing Vultr Inference key is missing or invalid; no key was printed.")
    require(1 <= len(model) <= 120 and re.fullmatch(r"[A-Za-z0-9._/-]+", model) is not None, "The configured Vultr model name is invalid.")
    private_dir = Path("/etc/rewind")
    require(not private_dir.is_symlink(), "Rewind private configuration directory must not be linked.")
    env_path = private_dir / "rewind.env"
    check_managed(env_path, "# Rewind managed environment\n")
    previous = private_environment(env_path) if env_path.exists() else {}
    code = previous.get("REWIND_ACCESS_CODE") or secrets.token_urlsafe(24)
    require(re.fullmatch(r"[A-Za-z0-9_-]{20,128}", code) is not None, "Existing Rewind access code is unsupported.")
    try:
        user = pwd.getpwnam("rewind")
        require(user.pw_dir == str(state) and user.pw_shell in {"/usr/sbin/nologin", "/sbin/nologin"}, "An unrelated account named rewind exists.")
    except KeyError:
        user = None
    listeners = command(["ss", "-H", "-ltn", "sport", "=", ":18182"])
    if listeners:
        active = capture(["systemctl", "is-active", "rewind.service"], seconds=5)
        require(previous and Path("/etc/systemd/system/rewind.service").is_file() and active.exit_code == 0
                and active.stdout.strip() == b"active", "Port 18182 is already used by another service.")
    # Preflight above completes before runtime, account, service or proxy changes.
    command(["bash", str(SOURCE / "infra/install-gvisor.sh")], seconds=180)
    if user is None:
        command(["useradd", "--system", "--user-group", "--home-dir", str(state), "--shell", "/usr/sbin/nologin", "rewind"])
        user = pwd.getpwnam("rewind")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chown(state, user.pw_uid, user.pw_gid)
    state.chmod(0o700)
    releases.mkdir(mode=0o755, parents=True, exist_ok=True)
    releases.parent.chmod(0o755)
    releases.chmod(0o755)
    release = releases / manifest["source_sha256"][:16]
    if release.exists():
        require(not release.is_symlink(), "An existing release path is a symbolic link.")
        require(verify_source(release)["source_sha256"] == manifest["source_sha256"], "An existing release path differs.")
    else:
        stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=releases))
        try:
            for relative, metadata in manifest["files"].items():
                target = stage / relative
                target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                for directory in [target.parent, *target.parent.parents]:
                    if directory.is_relative_to(stage):
                        directory.chmod(0o755)
                target.write_bytes((SOURCE / relative).read_bytes())
                target.chmod(metadata["mode"])
            (stage / "release-manifest.json").write_bytes((SOURCE / "release-manifest.json").read_bytes())
            (stage / "release-manifest.json").chmod(0o644)
            stage.chmod(0o755)
            verify_source(stage)
            os.rename(stage, release)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    private_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    settings = {"VULTR_INFERENCE_API_KEY": key, "VULTR_INFERENCE_MODEL": model, "REWIND_ACCESS_CODE": code,
        "REWIND_ORIGIN": "https://" + host, "REWIND_HOST": "127.0.0.1", "REWIND_PORT": "18182",
        "REWIND_STATE_DIR": str(state),
        "REWIND_PUBLIC_DEMO": "1" if args.public_demo else previous.get("REWIND_PUBLIC_DEMO", "0")}
    raw_env = "# Rewind managed environment\n" + "".join(name + "=" + json.dumps(value) + "\n" for name,value in settings.items())
    atomic_write(env_path, raw_env.encode(), private=True)
    atomic_write(private_dir / "reviewer-access.txt", (code + "\n").encode(), private=True)
    require(not link.exists() or link.is_symlink(), "The current release path contains an unrelated directory.")
    temporary_link = releases.parent / (".current-" + secrets.token_hex(8))
    temporary_link.symlink_to(release)
    os.replace(temporary_link, link)
    for name in MANAGED_FILES:
        atomic_write(Path("/etc/systemd/system") / name, (SOURCE / "infra" / name).read_bytes())
    command(["systemd-analyze", "verify", *[str(Path("/etc/systemd/system")/name) for name in sorted(MANAGED_FILES)]])
    command(["systemctl", "daemon-reload"])
    command(["systemctl", "enable", "rewind.service", "rewind-gc.timer"])
    command(["systemctl", "restart", "rewind.service"])
    command(["systemctl", "start", "rewind-gc.timer"])
    ready = False
    for attempt in range(25):
        try:
            with urllib.request.urlopen("http://127.0.0.1:18182/api/config", timeout=2) as response:
                ready = json.load(response).get("available") is True
            if ready:
                break
        except (OSError, ValueError):
            pass
        time.sleep(1)
    require(ready, "Rewind did not become ready; inspect its service privately. The existing HTTPS config was preserved.")
    if proposed_proxy != original_proxy:
        require(config.read_bytes() == original_proxy, "Caddy config changed concurrently; no proxy edit was applied.")
        atomic_write(private_dir / "caddy-before.private", original_proxy, private=True)
        atomic_write(config, proposed_proxy, proxy_metadata)
        try:
            command(["systemctl", "reload", args.caddy_service])
        except RuntimeError:
            require(config.read_bytes() == proposed_proxy, "A concurrent Caddy edit prevents automatic rollback.")
            atomic_write(config, original_proxy, proxy_metadata)
            command(["systemctl", "reload", args.caddy_service])
            raise RuntimeError("Rewind route reload failed; the original HTTPS configuration was restored.") from None
    require(command(["systemctl", "is-active", args.caddy_service]) == "active", "HTTPS service availability was not confirmed.")
    report = {"source_sha256":manifest["source_sha256"],"url":"https://"+host,"private_reviewer_code_file":str(private_dir/"reviewer-access.txt"),
        "existing_proxy_bytes_preserved":proposed_proxy.startswith(original_proxy),"live_inference_recorded":False,
        "public_demo":settings["REWIND_PUBLIC_DEMO"] == "1","checked_at":int(time.time())}
    atomic_write(private_dir / "deployment.json", canonical(report)+b"\n", private=True)
    print("Rewind is ready at https://" + host + ". Existing HTTPS configuration was preserved.")
    if settings["REWIND_PUBLIC_DEMO"] == "1":
        print("Public demo access is enabled without a shared code or daily trial quota. Each workspace still needs its private capability.")
    else:
        print("Read the demo access code privately from /etc/rewind/reviewer-access.txt.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hostname", required=True)
    parser.add_argument("--provider-env", type=Path, required=True)
    parser.add_argument("--caddy-config", type=Path, required=True)
    parser.add_argument("--caddy-service", required=True)
    parser.add_argument("--public-demo", action="store_true", help="Enable bounded public access without a shared demo code.")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        install(args)
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, "Rewind installation stopped: " + str(error) + "\n")


if __name__ == "__main__":
    main()
