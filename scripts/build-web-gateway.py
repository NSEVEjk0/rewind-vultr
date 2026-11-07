"""Assemble public media and an external Vercel rewrite for the full app."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urlsplit


def origin(value):
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Origins must not contain whitespace or control characters.")
    parts = urlsplit(value)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.port not in {None, 443} or parts.path not in {"", "/"} or parts.query or parts.fragment
            or not re.fullmatch(r"[A-Za-z0-9.-]+", parts.hostname)):
        raise ValueError("Use an HTTPS origin without credentials, a path, query or fragment.")
    return "https://" + parts.netloc.lower()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True, type=origin)
    parser.add_argument("--upstream", required=True, type=origin)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--tour", action="store_true", help="Keep the Shortlist preview under /tour/.")
    args = parser.parse_args()
    os.umask(0o077)
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    if output.exists() or output == root or root.is_relative_to(output):
        raise ValueError("Choose a fresh output directory that does not contain the repository.")
    subprocess.run(["git", "diff", "--quiet"], cwd=root, check=True)
    subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=root, check=True)
    preview = root / "preview"
    for path in preview.rglob("*"):
        if path.is_symlink():
            raise ValueError("Public assets must not contain symbolic links.")
    output.mkdir(parents=True, mode=0o700)
    shutil.copytree(preview / "demo", output / "demo")
    redirects = [{"source": "/demo", "destination": "/demo/", "permanent": False}]
    if args.tour:
        tour = output / "tour"
        tour.mkdir(mode=0o700)
        for path in preview.iterdir():
            if path.name in {"demo", "vercel.json", "source.zip"} or path.name.startswith("."):
                continue
            if path.is_dir():
                shutil.copytree(path, tour / path.name)
            else:
                shutil.copy2(path, tour / path.name)
        html = (tour / "index.html").read_text()
        for name in ["favicon.svg", "style.css", "app.js"]:
            html = html.replace('"/' + name + '"', '"/tour/' + name + '"')
        (tour / "index.html").write_text(html)
        js = (tour / "app.js").read_text().replace("'/examples/tour.json'", "'/tour/examples/tour.json'")
        (tour / "app.js").write_text(js)
        redirects += [{"source": "/", "destination": "/home", "permanent": False},
                      {"source": "/tour", "destination": "/tour/", "permanent": False}]
    config = {"framework": None, "buildCommand": "", "outputDirectory": ".",
              "redirects": redirects,
              "rewrites": [{"source": "/:path*", "destination": args.upstream + "/__web/:path*"}],
              "headers": [{"source": "/(.*)", "headers": [
                  {"key": "Cache-Control", "value": "no-store"},
                  {"key": "Vercel-CDN-Cache-Control", "value": "no-store"},
                  {"key": "X-Content-Type-Options", "value": "nosniff"},
                  {"key": "Referrer-Policy", "value": "no-referrer"}]}]}
    (output / "vercel.json").write_text(json.dumps(config, indent=2) + "\n")
    subprocess.run(["git", "archive", "--format=zip", "--prefix=" + root.name + "/",
                    "--output=" + str(output / "source.zip"), "HEAD"], cwd=root, check=True)
    hashes = {str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted(output.rglob("*")) if path.is_file()}
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    print(json.dumps({"origin": args.origin, "upstream": args.upstream, "output": str(output),
                      "source_commit": commit, "file_count": len(hashes), "hashes": hashes}))


if __name__ == "__main__":
    main()
