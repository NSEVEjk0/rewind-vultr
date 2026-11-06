"""Acceptance rules run in the controller, outside candidate execution.

This module operates on JSON from trusted inspections, never SQLite files.
"""
from __future__ import annotations
from .evidence import canonical, sha256_bytes


class ScopeError(ValueError):
    pass


def key_token(key: dict) -> str:
    return sha256_bytes(canonical(key))


def row_map(snapshot: dict) -> dict:
    result = {key_token(row["key"]): row for row in snapshot["rows"]}
    if len(result) != snapshot["row_count"]:
        raise ScopeError("The inspected row count or primary keys are inconsistent.")
    return result


def compatible(backup: dict, current: dict) -> None:
    if (backup["schema"] != current["schema"] or backup["columns"] != current["columns"]
            or backup["table"] != current["table"] or backup["primary_key"] != current["primary_key"]):
        raise ScopeError("The two snapshots must have the same table, columns and primary key.")
    if not backup.get("integrity") or not current.get("integrity"):
        raise ScopeError("Both snapshots must pass their integrity checks.")
    row_map(backup)
    row_map(current)


def compare(backup: dict, current: dict, columns: list[str]) -> dict:
    compatible(backup, current)
    names = {column["name"] for column in current["columns"]}
    if (not isinstance(columns, list) or not columns or len(set(columns)) != len(columns)
            or any(not isinstance(name, str) or name not in names or name == current["primary_key"] for name in columns)):
        raise ScopeError("Select ordinary columns to restore. The primary key must stay unchanged.")
    old, now = row_map(backup), row_map(current)
    choices = []
    for token, row in now.items():
        if token not in old:
            continue
        differences = [{"column": name, "current": row["values"][name], "backup": old[token]["values"][name]}
                       for name in columns if row["values"][name] != old[token]["values"][name]]
        if differences:
            choices.append({"token": token, "key": row["key"], "differences": differences})
    return {"columns": columns, "choices": choices, "new_rows": len(set(now) - set(old)),
            "current_rows": len(now), "backup_rows": len(old)}


def make_plan(backup: dict, current: dict, columns: list[str], keys: list[str]) -> dict:
    comparison = compare(backup, current, columns)
    choices = {row["token"]: row for row in comparison["choices"]}
    if (not isinstance(keys, list) or not 1 <= len(keys) <= 1000 or len(set(keys)) != len(keys)
            or any(not isinstance(key, str) or key not in choices for key in keys)):
        raise ScopeError("Select at least one changed record that exists in both snapshots.")
    old, now = row_map(backup), row_map(current)
    selected = set(keys)
    targets = []
    protected_updates = []
    for token, row in now.items():
        if token not in old:
            continue
        for name in row["values"]:
            if row["values"][name] == old[token]["values"][name]:
                continue
            item = {"token": token, "key": row["key"], "column": name,
                    "current": row["values"][name], "backup": old[token]["values"][name]}
            (targets if token in selected and name in columns else protected_updates).append(item)
    if not targets:
        raise ScopeError("The selected cells already match the backup.")
    scope = {"table": current["table"], "primary_key": current["primary_key"], "columns": columns,
             "keys": [now[token]["key"] for token in sorted(keys)]}
    plan = {"scope": scope, "targets": targets, "target_count": len(targets), "selected_rows": len(keys),
            "protected_updates": protected_updates, "protected_update_count": len(protected_updates),
            "new_rows": comparison["new_rows"], "current_rows": comparison["current_rows"],
            "backup_rows": comparison["backup_rows"], "backup_sha256": backup["file_sha256"],
            "current_sha256": current["file_sha256"],
            "reference": {"label": "Full backup replacement — reference comparison, not an AI result",
                          "lost_current_rows": comparison["new_rows"], "reverted_protected_updates": len(protected_updates)}}
    plan["sha256"] = sha256_bytes(canonical(plan))
    return plan


def verify_candidate(backup: dict, current: dict, candidate: dict, plan: dict) -> dict:
    old, now, result = row_map(backup), row_map(current), row_map(candidate)
    allowed = {(key_token(key), name) for key in plan["scope"]["keys"] for name in plan["scope"]["columns"]}
    failed_targets, failed_protected, diff = [], [], []
    for token, row in now.items():
        for name, before in row["values"].items():
            after = result.get(token, {}).get("values", {}).get(name)
            is_target = (token, name) in allowed
            expected = old[token]["values"][name] if is_target else before
            if after != expected:
                (failed_targets if is_target else failed_protected).append({"key": row["key"], "column": name})
            if before != after:
                diff.append({"key": row["key"], "column": name, "before": before, "after": after, "allowed": is_target})
    checks = [
        {"id": "sqlite-integrity", "label": "Database integrity and foreign keys pass", "ok": candidate.get("integrity") is True},
        {"id": "same-schema", "label": "Table definitions and columns are unchanged",
         "ok": candidate["schema"] == current["schema"] and candidate["columns"] == current["columns"]},
        {"id": "same-records", "label": "Every current record is retained, with no added records", "ok": set(result) == set(now)},
        {"id": "same-sequences", "label": "Automatic ID counters are unchanged", "ok": candidate.get("sequence", []) == current.get("sequence", [])},
        {"id": "restored-cells", "label": "Selected cells match the good backup", "ok": not failed_targets},
        {"id": "protected-cells", "label": "Every unselected cell keeps today's value", "ok": not failed_protected},
    ]
    return {"ok": all(check["ok"] for check in checks), "checks": checks, "diff": diff,
            "restored_cells": sum(item["allowed"] for item in diff), "retained_rows": len(set(result) & set(now)),
            "retained_new_rows": len((set(now) - set(old)) & set(result)),
            "protected_update_count": plan["protected_update_count"],
            "failures": {"target_cells": failed_targets[:20], "protected_cells": failed_protected[:20],
                         "missing_rows": len(set(now) - set(result)), "unexpected_rows": len(set(result) - set(now))}}
