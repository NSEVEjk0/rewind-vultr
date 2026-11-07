"""Fixed worker. This module is invoked ONLY inside the pinned gVisor image.

Candidate execution and trusted inspection always use different containers.
The candidate run has no acceptance answers or inspector state.
"""
from __future__ import annotations
import base64
import csv
import hashlib
import importlib.util
import io
import json
import math
from pathlib import Path
import shutil
import sqlite3
import sys

MAX_FILE = 256 * 1024
MAX_ROWS = 1000
MAX_COLUMNS = 16


def quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def cell(value):
    if value is None:
        return {"type": "null", "value": None}
    if type(value) is int:
        return {"type": "integer", "value": str(value)}
    if type(value) is float and math.isfinite(value):
        return {"type": "real", "value": repr(value)}
    if isinstance(value, str) and len(value) <= 512:
        return {"type": "text", "value": value}
    raise ValueError("Use short text, finite numbers or null values; blobs are unsupported.")


def inspect_database(path: Path) -> dict:
    raw = path.read_bytes()
    if not 512 <= len(raw) <= MAX_FILE or not raw.startswith(b"SQLite format 3\x00"):
        raise ValueError("Choose a SQLite snapshot up to 256 KiB.")
    if raw[18] == 2 or raw[19] == 2:
        raise ValueError("Export a consistent SQLite backup with the Online Backup API; WAL database files alone are unsupported.")
    conn = sqlite3.connect("file:" + str(path) + "?mode=ro&immutable=1", uri=True, timeout=1)
    try:
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.execute("PRAGMA query_only=ON")
        steps = [0]
        def progress():
            steps[0] += 1
            return int(steps[0] > 1200)
        conn.set_progress_handler(progress, 1000)
        integrity = [row[0] for row in conn.execute("PRAGMA integrity_check(1)")]
        if integrity != ["ok"] or list(conn.execute("PRAGMA foreign_key_check")):
            raise ValueError("This snapshot did not pass SQLite integrity and foreign-key checks.")
        objects = list(conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"))
        tables = [item for item in objects if item[0] == "table"]
        if len(tables) != 1 or any(item[0] not in {"table", "index"} for item in objects):
            raise ValueError("This version supports one ordinary table and no triggers or views.")
        _, table, _, sql = tables[0]
        if not sql or "VIRTUAL TABLE" in sql.upper() or not 1 <= len(table) <= 100:
            raise ValueError("Virtual tables and oversized identifiers are unsupported.")
        info = list(conn.execute("PRAGMA table_xinfo(" + quoted(table) + ")"))
        if not 2 <= len(info) <= MAX_COLUMNS or any(row[6] != 0 or not 1 <= len(row[1]) <= 100 for row in info):
            raise ValueError("Use 2–16 ordinary columns, without generated columns.")
        keys = [row[1] for row in info if row[5] > 0]
        if len(keys) != 1:
            raise ValueError("Choose a table with one stable integer or text primary key.")
        names = [row[1] for row in info]
        rows = []
        seen = set()
        for record in conn.execute("SELECT " + ",".join(quoted(name) for name in names) + " FROM " + quoted(table)):
            values = {name: cell(value) for name, value in zip(names, record)}
            key = values[keys[0]]
            if key["type"] not in {"integer", "text"}:
                raise ValueError("Primary keys must be non-null integers or text.")
            identity = json.dumps(key, sort_keys=True, separators=(",", ":"))
            if identity in seen:
                raise ValueError("Primary keys must be unique.")
            seen.add(identity)
            rows.append({"key": key, "values": values})
            if len(rows) > MAX_ROWS:
                raise ValueError("This version supports up to 1,000 rows.")
        if not rows:
            raise ValueError("Choose snapshots with at least one row.")
        rows.sort(key=lambda row: json.dumps(row["key"], sort_keys=True))
        sequence = []
        if conn.execute("SELECT 1 FROM sqlite_schema WHERE name='sqlite_sequence'").fetchone():
            sequence = [[cell(name), cell(seq)] for name, seq in conn.execute("SELECT name,seq FROM sqlite_sequence ORDER BY name")]
        return {"format": "sqlite", "file_sha256": hashlib.sha256(raw).hexdigest(), "table": table,
                "primary_key": keys[0], "schema": [list(item) for item in objects],
                "columns": [{"name": row[1], "declared_type": row[2], "not_null": bool(row[3]), "default": row[4], "primary_key": bool(row[5])} for row in info],
                "row_count": len(rows), "rows": rows, "sequence": sequence, "integrity": True}
    finally:
        conn.close()


def inspect_csv(path: Path, primary_key: str) -> dict:
    raw = path.read_bytes()
    if not 1 <= len(raw) <= MAX_FILE or b"\x00" in raw:
        raise ValueError("Choose a UTF-8 CSV file up to 256 KiB, without null bytes.")
    try:
        contents = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("CSV files must use UTF-8 encoding.") from error
    csv.field_size_limit(2048)
    reader = csv.reader(io.StringIO(contents, newline=""), strict=True)
    try:
        names = next(reader)
    except (StopIteration, csv.Error) as error:
        raise ValueError("Include a header row with 2–16 unique field names.") from error
    if (not 2 <= len(names) <= MAX_COLUMNS or len(set(names)) != len(names)
            or any(not 1 <= len(name) <= 100 or any(ord(char) < 32 for char in name) for name in names)):
        raise ValueError("Include 2–16 unique, nonempty field names, up to 100 characters each.")
    if not isinstance(primary_key, str) or primary_key not in names:
        raise ValueError("Choose the CSV column containing a stable, unique record ID.")
    rows, seen = [], set()
    try:
        for record in reader:
            if len(record) != len(names) or any(len(value) > 512 for value in record):
                raise ValueError("Every CSV row must match the header and use values up to 512 characters.")
            values = {name: cell(value) for name, value in zip(names, record)}
            key = values[primary_key]
            if not key["value"] or key["value"] in seen:
                raise ValueError("CSV record IDs must be nonempty and unique in every version.")
            seen.add(key["value"])
            rows.append({"key": key, "values": values})
            if len(rows) > MAX_ROWS:
                raise ValueError("This version supports up to 1,000 CSV records.")
    except csv.Error as error:
        raise ValueError("The CSV quoting or field size is invalid.") from error
    if not rows:
        raise ValueError("Choose CSV versions with at least one record.")
    return {"format": "csv", "file_sha256": hashlib.sha256(raw).hexdigest(), "table": "records",
            "primary_key": primary_key, "schema": [["csv", *names]],
            "columns": [{"name": name, "declared_type": "TEXT", "not_null": False, "default": None,
                         "primary_key": name == primary_key} for name in names],
            "row_count": len(rows), "rows": rows, "sequence": [], "integrity": True}


def inspect_file(path: Path, context: dict) -> dict:
    return inspect_csv(path, context.get("primary_key", "")) if context.get("format") == "csv" else inspect_database(path)


def csv_example() -> dict:
    names = ["invoice_id", "customer", "price", "payment_status", "created_on"]
    good, incident = [], []
    def amount(cents):
        return str(cents // 100) + "." + str(cents % 100).zfill(2)
    for number in range(1, 121):
        row = ["INV-" + str(number).zfill(4), "Sample customer " + str(number), amount(2500 + number * 125), "unpaid", "2026-11-05"]
        good.append(row)
        damaged = list(row)
        damaged[2] = amount((2500 + number * 125) * 100)
        incident.append(damaged)
    today = [list(row) for row in incident]
    for row in today[:20]:
        row[3] = "paid"
    for number, cents in [(3, 3100), (42, 7900), (88, 13750)]:
        today[number - 1][2] = amount(cents)
    for number in range(121, 128):
        today.append(["INV-" + str(number).zfill(4), "Sample customer " + str(number),
                      amount(3500 + number * 125), "unpaid", "2026-11-06"])
    files = {}
    for name, records in [("backup", good), ("checkpoint", incident), ("current", today)]:
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\r\n")
        writer.writerow(names)
        writer.writerows(records)
        files[name] = base64.b64encode(stream.getvalue().encode()).decode()
    return files


def demo_files(context: dict | None = None) -> dict:
    context = context or {}
    if context.get("scenario") == "preflight":
        files = {}
        names = ["product_id", "product", "price", "payment_status"]
        today, incoming = [], []
        for number in range(1, 25):
            price = str(20 + number) + ".00"
            today.append([str(number).zfill(4), "Sample product " + str(number), price, "paid"])
            incoming.append([str(number).zfill(4), "Sample product " + str(number),
                             str(22 + number) + ".00", "unpaid"])
        incoming[0][2] = "2100.00"
        incoming[1][2] = ""
        incoming.append(["9999", "Unknown product", "30.00", "unpaid"])
        for name, records in [("current", today), ("incoming", incoming)]:
            stream = io.StringIO(newline="")
            writer = csv.writer(stream, lineterminator="\r\n")
            writer.writerow(names); writer.writerows(records)
            files[name] = base64.b64encode(stream.getvalue().encode()).decode()
        return files
    if context.get("format") == "csv":
        return csv_example()
    backup = Path("/tmp/backup.sqlite")
    checkpoint = Path("/tmp/checkpoint.sqlite")
    current = Path("/tmp/current.sqlite")
    with sqlite3.connect(backup) as db:
        db.execute("CREATE TABLE invoices (invoice_id INTEGER PRIMARY KEY, customer TEXT NOT NULL, price_cents INTEGER NOT NULL, payment_status TEXT NOT NULL, created_on TEXT NOT NULL)")
        for invoice_id in range(1, 121):
            db.execute("INSERT INTO invoices VALUES (?,?,?,?,?)", (invoice_id, "Example buyer " + str(invoice_id), 2500 + invoice_id * 125, "unpaid", "2026-11-05"))
    shutil.copyfile(backup, current)
    with sqlite3.connect(current) as db:
        db.execute("UPDATE invoices SET price_cents=price_cents*100")
    if context.get("scenario") == "overlap":
        shutil.copyfile(current, checkpoint)
    with sqlite3.connect(current) as db:
        db.execute("UPDATE invoices SET payment_status='paid' WHERE invoice_id<=20")
        for invoice_id in range(121, 128):
            db.execute("INSERT INTO invoices VALUES (?,?,?,?,?)", (invoice_id, "Example buyer " + str(invoice_id), 3500 + invoice_id * 125, "unpaid", "2026-11-06"))
        if context.get("scenario") == "overlap":
            for invoice_id, cents in [(3, 3100), (42, 7900), (88, 13750)]:
                db.execute("UPDATE invoices SET price_cents=? WHERE invoice_id=?", (cents, invoice_id))
    paths = [("backup", backup), ("current", current)]
    if checkpoint.exists():
        paths.append(("checkpoint", checkpoint))
    return {name: base64.b64encode(path.read_bytes()).decode() for name, path in paths}


def run_candidate(scope: dict) -> dict:
    spec = importlib.util.spec_from_file_location("recovery_candidate", "/work/repair.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    function = getattr(module, "recover", None)
    if not callable(function):
        raise ValueError("The recovery program must define recover(backup_path, current_path, output_path, scope).")
    extension = "csv" if scope.get("format") == "csv" else "sqlite"
    output = Path("/tmp/recovered." + extension)
    function("/work/backup." + extension, "/work/current." + extension, str(output), scope)
    if not output.is_file() or output.is_symlink():
        raise ValueError("The recovery program did not produce a regular database file.")
    raw = output.read_bytes()
    if not (1 if extension == "csv" else 512) <= len(raw) <= MAX_FILE:
        raise ValueError("The recovered file exceeds its size limit.")
    return {"artifact": base64.b64encode(raw).decode()}


def run_diagnostic(context: dict) -> dict:
    spec = importlib.util.spec_from_file_location("diagnostic_candidate", "/work/diagnose.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    function = getattr(module, "diagnose", None)
    if not callable(function):
        raise ValueError("Define diagnose(backup_path, checkpoint_path, current_path, context).")
    extension = "csv" if context.get("format") == "csv" else "sqlite"
    result = function("/work/backup." + extension, "/work/checkpoint." + extension,
                      "/work/current." + extension, context)
    if not isinstance(result, dict):
        raise ValueError("The investigation must return a JSON object.")
    return result


def write_csv_artifact(names: list[str], rows: list[dict]) -> dict:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow(names)
    writer.writerows([[row["values"][name]["value"] for name in names] for row in rows])
    raw = stream.getvalue().encode("utf-8")
    if not 1 <= len(raw) <= MAX_FILE:
        raise ValueError("The checked CSV exceeds the file size limit.")
    return {"artifact": base64.b64encode(raw).decode()}


def run_import(context: dict, *, prepare: bool) -> dict:
    if context.get("contract") != "update-existing-by-id-v1":
        raise ValueError("Use the supported update-existing-records import contract.")
    key = context["primary_key"]
    current = inspect_csv(Path("/work/current.csv"), key)
    incoming = inspect_csv(Path("/work/incoming.csv"), key)
    names = [column["name"] for column in current["columns"]]
    fields = [column["name"] for column in incoming["columns"]]
    if not set(fields) <= set(names):
        raise ValueError("Every import field must exist in today's export.")
    now = {row["key"]["value"]: row for row in current["rows"]}
    next_rows = {row["key"]["value"]: row for row in incoming["rows"]}
    if prepare:
        cells = context.get("cells")
        if not isinstance(cells, list) or not 1 <= len(cells) <= 1000:
            raise ValueError("Use a bounded exact import selection.")
        selected_fields, selected_keys, seen = set(), set(), set()
        for item in cells:
            identity, name = item["key"]["value"], item["column"]
            if (item["key"]["type"] != "text" or identity not in now or identity not in next_rows
                    or name not in fields or name == key or (identity, name) in seen):
                raise ValueError("An approved import cell is invalid.")
            seen.add((identity, name)); selected_fields.add(name); selected_keys.add(identity)
            now[identity]["values"][name] = next_rows[identity]["values"][name]
        names = [key, *[name for name in names if name in selected_fields]]
        rows = [row for row in current["rows"] if row["key"]["value"] in selected_keys]
    else:
        if set(next_rows) - set(now):
            raise ValueError("New IDs cannot be added by this update-only import.")
        for identity, row in next_rows.items():
            for name in fields:
                if name != key:
                    now[identity]["values"][name] = row["values"][name]
        rows = current["rows"]
    return write_csv_artifact(names, rows)


def main():
    request = json.loads(sys.stdin.buffer.read(128 * 1024 + 1))
    nonce = request["nonce"]
    operation = request.get("operation", "")
    try:
        if operation == "inspect":
            context = request["scope"]
            payload = inspect_file(Path("/work/current." + ("csv" if context.get("format") == "csv" else "sqlite")), context)
        elif operation == "demo":
            payload = demo_files(request["scope"])
        elif operation == "candidate":
            payload = run_candidate(request["scope"])
        elif operation == "diagnose":
            payload = run_diagnostic(request["scope"])
        elif operation in {"prepare-import", "import-preview"}:
            payload = run_import(request["scope"], prepare=operation == "prepare-import")
        else:
            raise ValueError("Unsupported worker operation.")
        response = {"nonce": nonce, "ok": True, "payload": payload}
    except Exception as error:
        # No file contents, customer data, provider credentials or traceback.
        # Exception messages from generated code are untrusted and may contain rows.
        safe = str(error)[:300] if operation in {"inspect", "demo"} and isinstance(error, (ValueError, sqlite3.DatabaseError)) else "The isolated program could not complete this operation."
        response = {"nonce": nonce, "ok": False, "error": safe}
    print(json.dumps(response, ensure_ascii=False, separators=(",", ":"), allow_nan=False))


if __name__ == "__main__":
    main()
