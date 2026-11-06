"""Package reviewed source only; never include databases, keys or recordings."""
from pathlib import Path
import hashlib
import io
import json
import sys
import tarfile
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rewind.evidence import canonical, sha256_bytes

ROOT = Path(__file__).resolve().parent.parent
TOP = {"AGENTS.md", "README.md", "LICENSE", "THIRD_PARTY.md", "pyproject.toml", "package.json", "package-lock.json", ".gitignore", ".env.example"}
FOLDERS = {"rewind", "web", "infra", "scripts", "tests", "presentation"}
SUFFIXES = {".py", ".mjs", ".html", ".css", ".js", ".svg", ".sh", ".service", ".timer", ".md", ".pdf", ".vtt", ".json"}


def main():
    files = {}
    for name in sorted(TOP):
        path = ROOT / name
        if path.is_file():
            files[name] = path
    for folder in sorted(FOLDERS):
        for path in sorted((ROOT / folder).rglob("*")):
            relative = path.relative_to(ROOT)
            if path.is_file() and path.suffix in SUFFIXES and not any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
                if path.is_symlink():
                    raise RuntimeError("Linked release files are unsupported.")
                files[str(relative)] = path
    digests = {name: sha256_bytes(path.read_bytes()) for name,path in files.items()}
    source = sha256_bytes(canonical(digests))
    metadata = {name:{"sha256":digests[name],"bytes":path.stat().st_size,"mode":0o755 if path.suffix == ".sh" else 0o644} for name,path in files.items()}
    manifest = {"format":1,"source_sha256":source,"files":metadata}
    output = ROOT / "reports/release"
    output.mkdir(mode=0o700,parents=True,exist_ok=True)
    archive = output / ("rewind-" + source[:16] + ".tar.gz")
    prefix = "rewind-" + source[:16]
    with tarfile.open(archive,"w:gz") as release:
        for name,path in files.items():
            info = release.gettarinfo(str(path),arcname=prefix+"/"+name)
            info.uid=info.gid=0; info.uname=info.gname="root"; info.mode=metadata[name]["mode"]
            with path.open("rb") as stream:
                release.addfile(info,stream)
        raw=canonical(manifest)+b"\n"
        info=tarfile.TarInfo(prefix+"/release-manifest.json")
        info.size=len(raw); info.mode=0o644
        release.addfile(info,io.BytesIO(raw))
    archive.chmod(0o600)
    result={"archive":str(archive),"archive_sha256":sha256_bytes(archive.read_bytes()),"source_sha256":source,"source_files":len(files),
        "private_files_included":False,"deployment_complete":False,"live_inference_recorded":False}
    (output/"release.json").write_text(json.dumps(result,indent=2)+"\n")
    print(json.dumps(result,indent=2))


if __name__=="__main__":
    main()
