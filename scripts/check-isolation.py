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
from rewind.contract import compare, incident_changes, make_plan, validate_diagnosis, verify_candidate
from rewind.pipeline import decode_file, inspect_input, public_run
from rewind.sandbox import Sandbox

REFERENCE = '''import shutil, sqlite3
def recover(backup_path, current_path, output_path, scope):
    def q(s): return '"' + s.replace('"', '""') + '"'
    shutil.copyfile(current_path, output_path)
    old = sqlite3.connect('file:' + backup_path + '?mode=ro&immutable=1', uri=True)
    new = sqlite3.connect(output_path)
    for item in scope['cells']:
        key = item['key']
        value = int(key['value']) if key['type'] == 'integer' else key['value']
        values = old.execute('SELECT ' + q(item['column']) + ' FROM ' + q(scope['table']) + ' WHERE ' + q(scope['primary_key']) + '=?', (value,)).fetchone()
        if values is None: raise ValueError('Missing backup key')
        new.execute('UPDATE ' + q(scope['table']) + ' SET ' + q(item['column']) + '=? WHERE ' + q(scope['primary_key']) + '=?', tuple(values) + (value,))
    new.commit(); new.close(); old.close()
'''
WHOLE_BACKUP = '''import shutil
def recover(backup_path, current_path, output_path, scope):
    shutil.copyfile(backup_path, output_path)
'''
CSV_REFERENCE = '''import csv
def recover(backup_path, current_path, output_path, scope):
    def read(path):
        with open(path, encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream)
            return reader.fieldnames, list(reader)
    _, backup = read(backup_path)
    fields, current = read(current_path)
    key = scope['primary_key']
    old = {row[key]: row for row in backup}
    now = {row[key]: row for row in current}
    for item in scope['cells']:
        identity = item['key']['value']
        now[identity][item['column']] = old[identity][item['column']]
    with open(output_path, 'w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader(); writer.writerows(current)
'''
BLIND_CSV = '''import csv
def recover(backup_path, current_path, output_path, scope):
    with open(backup_path, encoding='utf-8-sig', newline='') as stream:
        old = {row[scope['primary_key']]: row for row in csv.DictReader(stream)}
    with open(current_path, encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream); fields = reader.fieldnames; rows = list(reader)
    for row in rows:
        if row[scope['primary_key']] in old:
            for name in scope['columns']: row[name] = old[row[scope['primary_key']]][name]
    with open(output_path, 'w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
'''
DIAGNOSTIC_REFERENCE = '''import csv, sqlite3
def diagnose(backup_path, checkpoint_path, current_path, context):
    key = context['primary_key']
    def q(name): return '"' + name.replace('"', '""') + '"'
    def read(path):
        if context['format'] == 'csv':
            with open(path, encoding='utf-8-sig', newline='') as stream:
                return {row[key]: row for row in csv.DictReader(stream)}
        with sqlite3.connect('file:' + path + '?mode=ro&immutable=1', uri=True) as db:
            db.row_factory = sqlite3.Row
            return {row[key]: dict(row) for row in db.execute('SELECT * FROM ' + q(context['table']))}
    good, incident, today = map(read, [backup_path, checkpoint_path, current_path])
    field = 'price' if context['format'] == 'csv' else 'price_cents'
    cells = []
    for identity in today:
        if identity in good and identity in incident and good[identity][field] != incident[identity][field]:
            cells.append({'key': {'type': 'integer' if type(identity) is int else 'text', 'value': str(identity)}, 'column': field})
    return {'cells': cells, 'summary': 'Fixed reference investigation of changed prices, including later overlaps.'}
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
        upgrades = {}
        for format in ("csv", "sqlite"):
            demo = sandbox.run("demo", scope={"format": format, "scenario": "overlap"})
            require(demo["ok"], "Three-version example creation failed.")
            runs.append(public_run(demo))
            raw = {name: decode_file(demo["payload"][name], format) for name in ("backup", "checkpoint", "current")}
            inspected = {}
            for name in raw:
                inspected[name], run = inspect_input(raw[name], sandbox, format=format, primary_key="invoice_id")
                runs.append(run)
            good, incident, today = [inspected[name] for name in ("backup", "checkpoint", "current")]
            investigation = sandbox.run("diagnose", {**{name + "." + format: data for name, data in raw.items()},
                "diagnose.py": DIAGNOSTIC_REFERENCE.encode()}, scope={"format": format, "table": today["table"], "primary_key": today["primary_key"]})
            require(investigation["ok"], "Isolated reference investigation failed.")
            runs.append(public_run(investigation))
            proposal = validate_diagnosis(good, incident, today, investigation["payload"])
            require((len(proposal["targets"]), len(proposal["overlaps"])) == (117, 3), "Checkpoint overlap detection was incorrect.")
            plan = make_plan(good, today, targets=[{"token": item["token"], "column": item["column"]} for item in proposal["targets"]], checkpoint=incident)
            programs = [("selective-reference", CSV_REFERENCE if format == "csv" else REFERENCE, True),
                        ("full-backup-reference", WHOLE_BACKUP, False)]
            if format == "csv":
                programs.append(("blind-price-reference", BLIND_CSV, False))
            checks = {}
            for label, code, expected in programs:
                execution = sandbox.run("candidate", {"backup." + format: raw["backup"], "current." + format: raw["current"], "repair.py": code.encode()}, scope=plan["scope"])
                require(execution["ok"], "The " + format + " " + label + " did not return a file.")
                result, run = inspect_input(decode_file(execution["payload"]["artifact"], format), sandbox, format=format, primary_key="invoice_id")
                runs.extend([public_run(execution), run])
                verification = verify_candidate(good, today, result, plan)
                require(verification["ok"] is expected, "The " + format + " " + label + " violated the expected acceptance boundary.")
                if expected:
                    require((verification["restored_cells"], verification["retained_rows"], verification["retained_new_rows"],
                             verification["protected_update_count"], verification["protected_overlap_count"]) == (117, 127, 7, 23, 3),
                            "The three-version repair did not preserve the required records and updates.")
                checks[label] = verification
            upgrades[format] = {"diagnostic_source": "fixed-reference-program", "proposed_restorations": 117,
                                "protected_overlaps": 3, "outcomes": checks}
        csv_rejections = []
        invalid_csv = [
            ("duplicate-key", b"id,value\n001,a\n001,b\n"),
            ("empty-key", b"id,value\n,a\n"),
            ("duplicate-header", b"id,id\n001,2\n"),
            ("row-width", b"id,value\n001,a,extra\n"),
            ("invalid-utf8", b"id,value\n001,\xff\n"),
            ("invalid-quoting", b'id,value\n001,"unterminated\n'),
            ("large-cell", b"id,value\n001," + b"x"*513 + b"\n"),
        ]
        for label, data in invalid_csv:
            run = sandbox.run("inspect", {"current.csv": data}, scope={"format": "csv", "primary_key": "id"})
            require(not run["ok"], "Unsupported CSV was accepted: " + label)
            runs.append(public_run(run)); csv_rejections.append(label)
        literal_csv = b'\xef\xbb\xbfid,price,note\r\n0007,001.20,"quoted, value"\r\n0008,2.00,"line one\nline two"\r\n0009,3.0,"=SUM(1,2)"\r\n'
        literal, run = inspect_input(literal_csv, sandbox, format="csv", primary_key="id")
        runs.append(run)
        require(literal["rows"][0]["values"]["price"]["value"] == "001.20" and literal["rows"][0]["key"]["value"] == "0007"
                and literal["rows"][1]["values"]["note"]["value"] == "line one\nline two"
                and literal["rows"][2]["values"]["note"]["value"] == "=SUM(1,2)", "CSV literal representation was not preserved.")
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
            "reference_programs_only": True, "outcomes": outcomes, "checkpoint_workflows": upgrades,
            "csv_rejections": csv_rejections, "csv_literal_values_preserved": True, "sandbox_runs": runs}, indent=2))
        print("Actual gVisor checks passed: SQLite and CSV recovery, executed diagnosis, 117 repairs with 3 overlaps and 23 updates protected, rejected broad rollbacks, CSV validation, containment, limits and removal of all " + str(len(runs)) + " sandboxes.")


if __name__ == "__main__":
    main()
