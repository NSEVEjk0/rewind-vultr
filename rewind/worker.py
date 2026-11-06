"""Fixed worker. This module is invoked ONLY inside the pinned gVisor image.

Candidate execution and trusted inspection always use different containers.
The candidate run has no acceptance answers or inspector state.
"""
from __future__ import annotations
import base64
import hashlib
import importlib.util
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
        return {"file_sha256": hashlib.sha256(raw).hexdigest(), "table": table,
                "primary_key": keys[0], "schema": [list(item) for item in objects],
                "columns": [{"name": row[1], "declared_type": row[2], "not_null": bool(row[3]), "default": row[4], "primary_key": bool(row[5])} for row in info],
                "row_count": len(rows), "rows": rows, "sequence": sequence, "integrity": True}
    finally:
        conn.close()


def demo_files() -> dict:
    backup = Path("/tmp/backup.sqlite")
    current = Path("/tmp/current.sqlite")
    with sqlite3.connect(backup) as db:
        db.execute("CREATE TABLE invoices (invoice_id INTEGER PRIMARY KEY, customer TEXT NOT NULL, price_cents INTEGER NOT NULL, payment_status TEXT NOT NULL, created_on TEXT NOT NULL)")
        for invoice_id in range(1, 121):
            db.execute("INSERT INTO invoices VALUES (?,?,?,?,?)", (invoice_id, "Example buyer " + str(invoice_id), 2500 + invoice_id * 125, "unpaid", "2026-11-05"))
    shutil.copyfile(backup, current)
    with sqlite3.connect(current) as db:
        db.execute("UPDATE invoices SET price_cents=price_cents*100")
        db.execute("UPDATE invoices SET payment_status='paid' WHERE invoice_id<=20")
        for invoice_id in range(121, 128):
            db.execute("INSERT INTO invoices VALUES (?,?,?,?,?)", (invoice_id, "Example buyer " + str(invoice_id), 3500 + invoice_id * 125, "unpaid", "2026-11-06"))
    return {name: base64.b64encode(path.read_bytes()).decode() for name, path in [("backup", backup), ("current", current)]}


def run_candidate(scope: dict) -> dict:
    spec = importlib.util.spec_from_file_location("recovery_candidate", "/work/repair.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    function = getattr(module, "recover", None)
    if not callable(function):
        raise ValueError("The recovery program must define recover(backup_path, current_path, output_path, scope).")
    output = Path("/tmp/recovered.sqlite")
    function("/work/backup.sqlite", "/work/current.sqlite", str(output), scope)
    if not output.is_file() or output.is_symlink():
        raise ValueError("The recovery program did not produce a regular database file.")
    raw = output.read_bytes()
    if not 512 <= len(raw) <= MAX_FILE:
        raise ValueError("The candidate database exceeds its size limit.")
    return {"artifact": base64.b64encode(raw).decode()}


def main():
    request = json.loads(sys.stdin.buffer.read(128 * 1024 + 1))
    nonce = request["nonce"]
    try:
        operation = request["operation"]
        if operation == "inspect":
            payload = inspect_database(Path("/work/current.sqlite"))
        elif operation == "demo":
            payload = demo_files()
        elif operation == "candidate":
            payload = run_candidate(request["scope"])
        else:
            raise ValueError("Unsupported worker operation.")
        response = {"nonce": nonce, "ok": True, "payload": payload}
    except Exception as error:
        # No file contents, customer data, provider credentials or traceback.
        safe = str(error)[:300] if isinstance(error, (ValueError, sqlite3.DatabaseError)) else "The worker could not complete this operation."
        response = {"nonce": nonce, "ok": False, "error": safe}
    print(json.dumps(response, ensure_ascii=False, separators=(",", ":"), allow_nan=False))


if __name__ == "__main__":
    main()
