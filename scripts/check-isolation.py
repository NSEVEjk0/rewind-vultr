"""Operator integration checks. All SQL and candidate fixtures run in gVisor.

These explicit reference programs are tests, never live inference fallbacks.
"""
from pathlib import Path
import json
import os
import sys
import tempfile
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rewind.contract import compare, make_plan, verify_candidate
from rewind.pipeline import decode_file, inspect_input, public_run
from rewind.sandbox import Sandbox

REFERENCE = '''import shutil, sqlite3
def recover(backup_path, current_path, output_path, scope):
    def q(s): return '"' + s.replace('"', '""') + '"'
    shutil.copyfile(current_path, output_path)
    old = sqlite3.connect('file:' + backup_path + '?mode=ro&immutable=1', uri=True)
    new = sqlite3.connect(output_path)
    for key in scope['keys']:
        value = int(key['value']) if key['type'] == 'integer' else key['value']
        values = old.execute('SELECT ' + ','.join(q(c) for c in scope['columns']) + ' FROM ' + q(scope['table']) + ' WHERE ' + q(scope['primary_key']) + '=?', (value,)).fetchone()
        if values is None: raise ValueError('Missing backup key')
        new.execute('UPDATE ' + q(scope['table']) + ' SET ' + ','.join(q(c) + '=?' for c in scope['columns']) + ' WHERE ' + q(scope['primary_key']) + '=?', tuple(values) + (value,))
    new.commit(); new.close(); old.close()
'''
WHOLE_BACKUP = '''import shutil
def recover(backup_path, current_path, output_path, scope):
    shutil.copyfile(backup_path, output_path)
'''

def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    root = Path(__file__).resolve().parent.parent
    output = root / "reports"
    output.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="rewind-isolation-") as temporary:
        sandbox = Sandbox(Path(temporary) / "work")
        sandbox.ready()  # Fail closed if runsc or the immutable image is missing.
        runs = []
        demo = sandbox.run("demo")
        require(demo["ok"], "Sandboxed demo-file creation failed.")
        runs.append(public_run(demo))
        backup_raw, current_raw = [decode_file(demo["payload"][name]) for name in ("backup", "current")]
        backup, backup_run = inspect_input(backup_raw, sandbox)
        current, current_run = inspect_input(current_raw, sandbox)
        runs.extend([backup_run, current_run])
        choices = compare(backup, current, ["price_cents"])["choices"]
        plan = make_plan(backup, current, ["price_cents"], [item["token"] for item in choices])
        outcomes = {}
        for label, code, expected in [("full-backup-reference", WHOLE_BACKUP, False), ("selective-reference", REFERENCE, True)]:
            candidate = sandbox.run("candidate", {"backup.sqlite": backup_raw, "current.sqlite": current_raw, "repair.py": code.encode()}, scope=plan["scope"])
            require(candidate["ok"], label + " did not return a bounded artifact.")
            artifact = decode_file(candidate["payload"]["artifact"])
            inspected, inspector_run = inspect_input(artifact, sandbox)
            runs.extend([public_run(candidate), inspector_run])
            verification = verify_candidate(backup, current, inspected, plan)
            require(verification["ok"] is expected, label + " had an unexpected result.")
            outcomes[label] = verification
        probe_code = '''import os, pathlib, socket, shutil
def recover(backup_path, current_path, output_path, scope):
    if os.getuid() != 65532: raise ValueError('Unexpected root user')
    if pathlib.Path('/var/run/docker.sock').exists(): raise ValueError('Docker socket was exposed')
    # The pinned Python image includes a public GPG_KEY used to verify Python.
    # Check actual controller credentials and a deliberately injected canary.
    private_names = {'VULTR_INFERENCE_API_KEY', 'AEGIS_API_KEY', 'REWIND_ACCESS_CODE', 'REWIND_CREDENTIAL_CANARY'}
    if private_names.intersection(os.environ): raise ValueError('Controller credential environment was exposed')
    try:
        with open(current_path, 'wb') as stream: stream.write(b'bad')
    except OSError: pass
    else: raise ValueError('Read-only input was writable')
    try: connection = socket.create_connection(('1.1.1.1', 443), timeout=1)
    except OSError: pass
    else: connection.close(); raise ValueError('Outbound network was reachable')
    shutil.copyfile(current_path, output_path)
'''
        with patch.dict(os.environ, {"REWIND_CREDENTIAL_CANARY": "public-isolation-test-canary"}):
            probe = sandbox.run("candidate", {"backup.sqlite": backup_raw, "current.sqlite": current_raw, "repair.py": probe_code.encode()}, scope=plan["scope"])
        require(probe["ok"], "The nonroot/no-network/read-only/no-credential containment probe failed.")
        require(decode_file(probe["payload"]["artifact"]) == current_raw, "The containment probe modified its input artifact.")
        runs.append(public_run(probe))
        for label, code, stop in [("deadline", "def recover(a,b,c,d):\n    while True: pass\n", "timeout"),
                                  ("output-limit", "def recover(a,b,c,d):\n    print('x'*500000)\n", "output_limit")]:
            run = sandbox.run("candidate", {"backup.sqlite": backup_raw, "current.sqlite": current_raw, "repair.py": code.encode()}, scope=plan["scope"], seconds=2)
            require(not run["ok"] and run["stop_reason"] == stop, label + " was not enforced.")
            runs.append(public_run(run))
        require(all(run["policy_verified"] and run["teardown"]["confirmed"] for run in runs), "One owned sandbox policy or teardown failed.")
        (output / "isolation-verification.json").write_text(json.dumps({"live_inference": False,
            "reference_programs_only": True, "outcomes": outcomes, "sandbox_runs": runs}, indent=2))
        print("Actual gVisor checks passed: selective recovery, rejected whole-backup replacement, nonroot/no-network/read-only/no-credential probes, deadline, output limit and confirmed removal.")


if __name__ == "__main__":
    main()
