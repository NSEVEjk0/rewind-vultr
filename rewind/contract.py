"""Acceptance rules over trusted JSON inspections, outside generated execution."""
from __future__ import annotations
from .evidence import canonical, sha256_bytes

MAX_TARGETS = 1000
MAX_SCOPE_BYTES = 112 * 1024


class ScopeError(ValueError):
    pass


def key_token(key: dict) -> str:
    return sha256_bytes(canonical(key))


def row_map(snapshot: dict) -> dict:
    result = {key_token(row["key"]): row for row in snapshot["rows"]}
    if len(result) != snapshot["row_count"]:
        raise ScopeError("The inspected record count or unique keys are inconsistent.")
    return result


def compatible(backup: dict, current: dict) -> None:
    if (backup.get("format", "sqlite") != current.get("format", "sqlite")
            or backup["schema"] != current["schema"] or backup["columns"] != current["columns"]
            or backup["table"] != current["table"] or backup["primary_key"] != current["primary_key"]):
        raise ScopeError("All versions must have the same format, table, columns and unique key.")
    if not backup.get("integrity") or not current.get("integrity"):
        raise ScopeError("All versions must pass their file checks.")
    row_map(backup)
    row_map(current)


def validate_columns(current: dict, columns: list[str]) -> None:
    names = {column["name"] for column in current["columns"]}
    if (not isinstance(columns, list) or not columns
            or any(not isinstance(name, str) or name not in names or name == current["primary_key"] for name in columns)
            or len(set(columns)) != len(columns)):
        raise ScopeError("Select ordinary fields to restore. The unique key must stay unchanged.")


def incident_changes(backup: dict, checkpoint: dict, current: dict) -> list[dict]:
    compatible(backup, checkpoint)
    compatible(backup, current)
    old, incident, now = row_map(backup), row_map(checkpoint), row_map(current)
    changes = []
    for token, row in now.items():
        if token not in old or token not in incident:
            continue
        for name in row["values"]:
            before, during, today = old[token]["values"][name], incident[token]["values"][name], row["values"][name]
            if name == current["primary_key"] or before == during:
                continue
            classification = "already-restored" if today == before else "overlap" if today != during else "restore"
            changes.append({"token": token, "key": row["key"], "column": name,
                            "backup": before, "checkpoint": during, "current": today,
                            "classification": classification})
    return changes


def diagnostic_metadata(backup: dict, checkpoint: dict, current: dict) -> dict:
    """Only structural metadata and aggregate counts may leave the controller."""
    changes = incident_changes(backup, checkpoint, current)
    incident, now = row_map(checkpoint), row_map(current)
    counts = []
    for column in current["columns"]:
        name = column["name"]
        if name == current["primary_key"]:
            continue
        affected = [item for item in changes if item["column"] == name]
        counts.append({"name": name, "declared_type": column["declared_type"],
                       "incident_changed_cells": len(affected),
                       "still_at_incident_value": sum(item["classification"] == "restore" for item in affected),
                       "changed_after_incident": sum(item["classification"] == "overlap" for item in affected),
                       "already_at_good_value": sum(item["classification"] == "already-restored" for item in affected),
                       "later_changed_cells": sum(row["values"][name] != incident[token]["values"][name]
                                                   for token, row in now.items() if token in incident)})
    return {"format": current.get("format", "sqlite"), "table": current["table"],
            "primary_key": current["primary_key"], "columns": counts,
            "record_counts": {"good": backup["row_count"], "after_incident": checkpoint["row_count"], "today": current["row_count"]},
            "new_records_since_incident": len(set(now) - set(incident)),
            "removed_records_since_incident": len(set(incident) - set(now))}


def validate_diagnosis(backup: dict, checkpoint: dict, current: dict, payload: dict) -> dict:
    if not isinstance(payload, dict) or set(payload) != {"cells", "summary"}:
        raise ScopeError("The investigation must return a bounded cell proposal and summary.")
    if not isinstance(payload["summary"], str) or not 1 <= len(payload["summary"]) <= 2000:
        raise ScopeError("The investigation summary exceeds its limit.")
    cells = payload["cells"]
    if not isinstance(cells, list) or not 1 <= len(cells) <= MAX_TARGETS:
        raise ScopeError("The investigation must propose 1–1,000 incident changes.")
    observed_changes = incident_changes(backup, checkpoint, current)
    observed = {(item["token"], item["column"]): item for item in observed_changes}
    selected, seen = [], set()
    for cell in cells:
        if not isinstance(cell, dict) or set(cell) != {"key", "column"} or not isinstance(cell["column"], str):
            raise ScopeError("The investigation proposed an invalid cell.")
        key = cell["key"]
        if (not isinstance(key, dict) or set(key) != {"type", "value"}
                or key["type"] not in {"integer", "text"} or not isinstance(key["value"], str) or len(key["value"]) > 512):
            raise ScopeError("The investigation proposed an invalid record key.")
        identity = (key_token(key), cell["column"])
        if identity not in observed or identity in seen:
            raise ScopeError("The investigation proposed a duplicate or unsupported incident change.")
        seen.add(identity)
        selected.append(observed[identity])
    selected.sort(key=lambda item: (item["token"], item["column"]))
    selected_columns = {item["column"] for item in selected}
    # A diagnostic cannot hide protected overlaps by omitting them. Display
    # observed overlaps in its proposed fields without expanding repair authority.
    protected = [item for item in observed_changes if item["column"] in selected_columns and item["classification"] == "overlap"]
    already = [item for item in observed_changes if item["column"] in selected_columns and item["classification"] == "already-restored"]
    result = {"summary": payload["summary"], "cells": selected,
              "columns": [col["name"] for col in current["columns"] if any(item["column"] == col["name"] for item in selected)],
              "targets": [item for item in selected if item["classification"] == "restore"],
              "overlaps": protected, "already_restored": already,
              "backup_sha256": backup["file_sha256"], "checkpoint_sha256": checkpoint["file_sha256"], "current_sha256": current["file_sha256"]}
    result["sha256"] = sha256_bytes(canonical(result))
    return result


def compare(backup: dict, current: dict, columns: list[str], checkpoint: dict | None = None) -> dict:
    compatible(backup, current)
    validate_columns(current, columns)
    old, now = row_map(backup), row_map(current)
    incident = row_map(checkpoint) if checkpoint else None
    if checkpoint:
        compatible(backup, checkpoint)
    choices, overlaps, already = [], [], []
    for token, row in now.items():
        if token not in old:
            continue
        differences = []
        for name in columns:
            before, today = old[token]["values"][name], row["values"][name]
            item = {"token": token, "key": row["key"], "column": name, "current": today, "backup": before}
            if incident is not None:
                if token not in incident or before == incident[token]["values"][name]:
                    continue
                item["checkpoint"] = incident[token]["values"][name]
                if today == before:
                    already.append(item)
                    continue
                if today != item["checkpoint"]:
                    overlaps.append(item)
                    continue
            elif today == before:
                continue
            differences.append(item)
        if differences:
            choices.append({"token": token, "key": row["key"], "differences": differences})
    return {"columns": columns, "choices": choices, "overlaps": overlaps, "already_restored": already,
            "new_rows": len(set(now) - set(old)), "current_rows": len(now), "backup_rows": len(old),
            "checkpoint_available": checkpoint is not None,
            "history_note": "Changes observed after the incident are protected. Snapshots cannot reveal edits that returned to the same value." if checkpoint else
                            "Without an after-incident version, Rewind cannot identify later edits to selected cells. Choose only damaged cells."}


def make_plan(backup: dict, current: dict, columns: list[str] | None = None, keys: list[str] | None = None,
              *, targets: list[dict] | None = None, checkpoint: dict | None = None,
              workspace_version: int | None = None, diagnosis_sha256: str | None = None) -> dict:
    if targets is not None:
        if (not isinstance(targets, list) or not 1 <= len(targets) <= MAX_TARGETS
                or any(not isinstance(item, dict) or set(item) != {"token", "column"}
                       or not isinstance(item["token"], str) or not isinstance(item["column"], str) for item in targets)):
            raise ScopeError("Approve 1–1,000 precise cells from the checked changes.")
        columns = [col["name"] for col in current["columns"] if any(item["column"] == col["name"] for item in targets)]
        selected = {(item["token"], item["column"]) for item in targets}
        if len(selected) != len(targets):
            raise ScopeError("Each approved cell must appear only once.")
    else:
        if (not isinstance(keys, list) or not 1 <= len(keys) <= MAX_TARGETS
                or any(not isinstance(key, str) for key in keys) or len(set(keys)) != len(keys)):
            raise ScopeError("Select at least one changed record that exists in both versions.")
        selected = {(token, name) for token in keys for name in columns or []}
    comparison = compare(backup, current, columns or [], checkpoint)
    choices = {(row["token"], item["column"]): item for row in comparison["choices"] for item in row["differences"]}
    if targets is not None:
        if not selected <= set(choices):
            raise ScopeError("The selection includes an unchanged, missing or protected later edit.")
    else:
        available_keys = {token for token, _ in choices}
        if not set(keys) <= available_keys:
            raise ScopeError("Select changed records from the checked comparison.")
        selected &= set(choices)
    if not selected or len(selected) > MAX_TARGETS:
        raise ScopeError("Approve 1–1,000 changed cells. Split a larger repair into smaller files.")
    approved = [choices[identity] for identity in sorted(selected)]
    old, now = row_map(backup), row_map(current)
    incident = row_map(checkpoint) if checkpoint else {}
    protected_updates, later = [], []
    for token, row in now.items():
        for name, value in row["values"].items():
            if name == current["primary_key"]:
                continue
            is_later = token in incident and value != incident[token]["values"][name]
            item = {"token": token, "key": row["key"], "column": name, "current": value}
            if is_later:
                later.append({**item, "checkpoint": incident[token]["values"][name]})
            if token in old and value != old[token]["values"][name] and (token, name) not in selected:
                protected_updates.append({**item, "backup": old[token]["values"][name],
                                          "reason": "Changed after incident" if is_later else "Outside approved cells"})
    columns = [col["name"] for col in current["columns"] if any(item["column"] == col["name"] for item in approved)]
    scope = {"format": current.get("format", "sqlite"), "table": current["table"], "primary_key": current["primary_key"],
             "columns": columns, "keys": [now[token]["key"] for token in sorted({token for token, _ in selected})],
             "cells": [{"key": item["key"], "column": item["column"]} for item in approved]}
    if len(canonical(scope)) > MAX_SCOPE_BYTES:
        raise ScopeError("This approval exceeds the bounded execution input. Select fewer cells.")
    plan = {"scope": scope, "targets": approved, "target_count": len(approved), "selected_rows": len(scope["keys"]),
            "protected_updates": protected_updates, "protected_update_count": len(protected_updates),
            "new_rows": comparison["new_rows"], "current_rows": comparison["current_rows"], "backup_rows": comparison["backup_rows"],
            "backup_sha256": backup["file_sha256"], "current_sha256": current["file_sha256"],
            "checkpoint_sha256": checkpoint["file_sha256"] if checkpoint else None,
            "workspace_version": workspace_version, "diagnosis_sha256": diagnosis_sha256,
            "history": {"checkpoint_available": checkpoint is not None, "note": comparison["history_note"],
                        "overlaps": comparison["overlaps"], "protected_overlap_count": len(comparison["overlaps"]),
                        "already_restored_count": len(comparison["already_restored"]),
                        "later_updates": later, "later_update_count": len(later)},
            "reference": {"label": "Whole backup replacement — reference comparison",
                          "lost_current_rows": comparison["new_rows"], "reverted_protected_updates": len(protected_updates)}}
    plan["sha256"] = sha256_bytes(canonical(plan))
    return plan


def rebind_plan(backup: dict, current: dict, plan: dict, checkpoint: dict | None = None) -> dict:
    return make_plan(backup, current, targets=[{"token": item["token"], "column": item["column"]} for item in plan["targets"]],
                     checkpoint=checkpoint, workspace_version=plan.get("workspace_version"), diagnosis_sha256=plan.get("diagnosis_sha256"))


def verify_candidate(backup: dict, current: dict, candidate: dict, plan: dict) -> dict:
    old, now, result = row_map(backup), row_map(current), row_map(candidate)
    # Authority is the explicit approved mask, never rows × columns.
    allowed = {(item["token"], item["column"]) for item in plan["targets"]}
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
    csv_format = current.get("format") == "csv"
    checks = [
        {"id": "csv-integrity" if csv_format else "sqlite-integrity", "label": "CSV structure and unique keys pass" if csv_format else "Database integrity and foreign keys pass", "ok": candidate.get("integrity") is True},
        {"id": "same-schema", "label": "File format, fields and unique key are unchanged",
         "ok": candidate.get("format", "sqlite") == current.get("format", "sqlite") and candidate["schema"] == current["schema"]
                and candidate["columns"] == current["columns"] and candidate["table"] == current["table"] and candidate["primary_key"] == current["primary_key"]},
        {"id": "same-records", "label": "Every current record is retained, with no added records", "ok": set(result) == set(now)},
        {"id": "same-sequences", "label": "Automatic ID counters are unchanged", "ok": candidate.get("sequence", []) == current.get("sequence", [])},
        {"id": "restored-cells", "label": "Every approved cell matches the good version", "ok": not failed_targets},
        {"id": "protected-cells", "label": "Every other cell keeps today's value", "ok": not failed_protected},
    ]
    if csv_format:
        checks.append({"id": "same-record-order", "label": "CSV records keep today's order",
                       "ok": [row["key"] for row in candidate["rows"]] == [row["key"] for row in current["rows"]]})
    return {"ok": all(check["ok"] for check in checks), "checks": checks, "diff": diff,
            "restored_cells": sum(item["allowed"] for item in diff), "retained_rows": len(set(result) & set(now)),
            "retained_new_rows": len((set(now) - set(old)) & set(result)), "protected_update_count": plan["protected_update_count"],
            "protected_overlap_count": plan.get("history", {}).get("protected_overlap_count", 0),
            "failures": {"target_cells": failed_targets[:20], "protected_cells": failed_protected[:20],
                         "missing_rows": len(set(now) - set(result)), "unexpected_rows": len(set(result) - set(now))}}
