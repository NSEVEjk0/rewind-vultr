"""Install the complete pinned gVisor bundle in the shared pinned runtime directory (adapted from ReproLab, MIT)."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import tarfile
import tempfile

ARCHIVE_SHA256 = '6f6b1a8f0f4665a403238135167d0d990bfaeaff5544abf68f5e0d822b35484a'
DESTINATION = Path('/opt/reprolab-runtime/release-20261102.0')
REQUIRED_FILES = {'runsc', 'gvisor-bin/gvisor_sentry'}


def require(ok: bool, message: str) -> None:
    if not ok:
        raise RuntimeError(message)


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def check_path(path: Path, *, directory: bool) -> None:
    for candidate in [path, *path.parents]:
        require(not candidate.is_symlink(), 'Runtime destination contains a symbolic link.')
    if path.exists():
        require(path.is_dir() if directory else path.is_file(),
                'Runtime destination has a conflicting file type.')


def make_directory(path: Path) -> None:
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    check_path(current, directory=True)
    for candidate in reversed(missing):
        candidate.mkdir(mode=0o755)
        candidate.chmod(0o755)  # Private installer umask must not hide runtime helpers.


def install_bundle(archive: Path, destination: Path, *,
                   expected_sha256: str = ARCHIVE_SHA256) -> dict:
    require(digest(archive) == expected_sha256, 'Pinned gVisor archive checksum mismatch.')
    destination = destination.absolute()
    with tempfile.TemporaryDirectory(prefix='rewind-gvisor-stage-') as directory:
        stage = Path(directory)
        files = {}
        directories = {PurePosixPath('.')}
        with tarfile.open(archive) as bundle:
            members = bundle.getmembers()
            seen = set()
            for member in members:
                name = PurePosixPath(member.name)
                require(not name.is_absolute() and '..' not in name.parts
                        and bool(name.parts) and str(name) == member.name.rstrip('/'),
                        'Unsafe path in gVisor archive.')
                require(member.isfile() or member.isdir(), 'Links or special files in gVisor archive.')
                require(name not in seen, 'Duplicate path in gVisor archive.')
                seen.add(name)
                directories.update(name.parents)
                if member.isdir():
                    directories.add(name)
                else:
                    files[name] = {'sha256': None, 'mode': 0o755 if member.mode & 0o111 else 0o644}
            require(REQUIRED_FILES.issubset({str(name) for name in files}),
                    'The gVisor bundle is incomplete: runsc and gvisor-bin/gvisor_sentry are required.')
            require(not set(files).intersection(directories), 'Conflicting paths in gVisor archive.')
            for member in members:
                name = PurePosixPath(member.name)
                if not member.isfile():
                    continue
                output = stage.joinpath(*name.parts)
                output.parent.mkdir(parents=True, exist_ok=True)
                stream = bundle.extractfile(member)
                require(stream is not None, 'Cannot read a pinned gVisor bundle member.')
                with stream, output.open('xb') as target:
                    shutil.copyfileobj(stream, target, length=1024 * 1024)
                output.chmod(files[name]['mode'])
                files[name]['sha256'] = digest(output)

        # Validate the entire existing destination before adding any files.
        for name in directories:
            check_path(destination.joinpath(*name.parts), directory=True)
        for name, metadata in files.items():
            target = destination.joinpath(*name.parts)
            check_path(target, directory=False)
            if target.exists():
                require(digest(target) == metadata['sha256'],
                        'An existing pinned runtime file differs; no files were overwritten.')
        for name in sorted(directories, key=lambda value: len(value.parts)):
            make_directory(destination.joinpath(*name.parts))
        installed = []
        for name, metadata in files.items():
            target = destination.joinpath(*name.parts)
            if not target.exists():
                fd, temporary = tempfile.mkstemp(prefix='.rewind-bundle-', dir=target.parent)
                try:
                    with os.fdopen(fd, 'wb') as output, stage.joinpath(*name.parts).open('rb') as source:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
                        os.fchmod(output.fileno(), metadata['mode'])
                        output.flush()
                        os.fsync(output.fileno())
                    # Atomic creation that cannot overwrite a concurrent operator edit.
                    os.link(temporary, target)
                    installed.append(str(name))
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            check_path(target, directory=False)
            require(digest(target) == metadata['sha256'], 'A runtime file changed during installation.')
            target.chmod(metadata['mode'])
        return {'archive_sha256': expected_sha256, 'destination': str(destination),
                'files': {str(name): value['sha256'] for name, value in files.items()},
                'installed': installed, 'complete_bundle': True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--destination', type=Path, default=DESTINATION)
    args = parser.parse_args()
    print(json.dumps(install_bundle(args.archive, args.destination), indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('gVisor bundle installation stopped: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
