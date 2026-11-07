"""Import rules over trusted inspections. Uploaded CSV is never parsed here."""
from __future__ import annotations
from decimal import Decimal, localcontext
import re
from .contract import MAX_SCOPE_BYTES, MAX_TARGETS, ScopeError, compatible, key_token, row_map
from .evidence import canonical, sha256_bytes

IMPORT_CONTRACT = "update-existing-by-id-v1"
REQUIRED_CHECKS = frozenset({
    "approved-csv-integrity", "approved-columns", "approved-records", "approved-record-order",
    "exact-import-values", "preview-csv-integrity", "same-schema", "same-records",
    "same-record-order", "approved-cells", "protected-cells", "input-fingerprints",
})
REASONS = {
    "unintended-field": "This field is outside the update you asked for.",
    "blank-overwrite": "A blank import value would erase an existing value.",
    "numeric-format": "The numeric check needs plain decimal numbers in both files.",
    "zero-baseline": "A change from zero needs a separate review.",
    "large-numeric-change": "This change exceeds your numeric limit.",
}
NUMBER = re.compile(r"[+-]?(?:[0-9]{1,32}(?:\.[0-9]{1,16})?|\.[0-9]{1,16})\Z")


def compatible_import(current: dict, incoming: dict) -> None:
    if (current.get("format") != "csv" or incoming.get("format") != "csv"
            or not current.get("integrity") or not incoming.get("integrity")
            or current["primary_key"] != incoming["primary_key"]
            or not {column["name"] for column in incoming["columns"]}
                   <= {column["name"] for column in current["columns"]}):
        raise ScopeError("Use CSV files with the same unique ID and import fields present in today's export.")
    row_map(current)
    row_map(incoming)


def import_policy(current: dict, incoming: dict, policy: dict) -> dict:
    compatible_import(current, incoming)
    if not isinstance(policy, dict) or set(policy) != {"columns", "numeric_column", "max_percent"}:
        raise ScopeError("Choose the intended fields and a numeric change limit.")
    columns = policy["columns"]
    available = {column["name"] for column in incoming["columns"] if not column["primary_key"]}
    if (not isinstance(columns, list) or not columns or len(columns) > 15
            or any(not isinstance(name, str) or name not in available for name in columns)
            or len(set(columns)) != len(columns)):
        raise ScopeError("Select at least one import field. The unique record ID cannot change.")
    numeric = policy["numeric_column"]
    if not isinstance(numeric, str) or numeric and numeric not in columns:
        raise ScopeError("Choose an intended field for the numeric check, or leave the numeric check off.")
    limit = policy["max_percent"]
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ScopeError("Use a numeric change limit from 1 to 1,000 percent.")
    return {"columns": sorted(columns), "numeric_column": numeric, "max_percent": limit}


def decimal_value(value: str) -> Decimal | None:
    return Decimal(value) if NUMBER.fullmatch(value) else None


def review_import(current: dict, incoming: dict, policy: dict) -> dict:
    policy = import_policy(current, incoming, policy)
    now, next_rows = row_map(current), row_map(incoming)
    changes, unknown = [], []
    for token, row in next_rows.items():
        if token not in now:
            unknown.append({"token": token, "key": row["key"],
                            "reason": "This ID is absent from today's export. It will be left out."})
            continue
        for name, after in row["values"].items():
            before = now[token]["values"][name]
            if name == current["primary_key"] or before == after:
                continue
            reasons, percent = [], None
            if name not in policy["columns"]:
                reasons.append("unintended-field")
            if before["value"].strip() and not after["value"].strip():
                reasons.append("blank-overwrite")
            if name == policy["numeric_column"] and "blank-overwrite" not in reasons:
                old, new = decimal_value(before["value"]), decimal_value(after["value"])
                if old is None or new is None:
                    reasons.append("numeric-format")
                elif not old and new:
                    reasons.append("zero-baseline")
                elif old:
                    with localcontext() as context:
                        context.prec = 112
                        delta = abs(new - old) * 100
                        change = delta / abs(old)
                        percent = format(change, ".2f")
                        if delta > abs(old) * policy["max_percent"]:
                            reasons.append("large-numeric-change")
            changes.append({"token": token, "key": row["key"], "column": name,
                            "before": before, "after": after, "reasons": reasons,
                            "percent_change": percent, "eligible": not reasons})
    return {"contract": IMPORT_CONTRACT, "policy": policy, "changes": changes,
            "unknown_ids": unknown, "current_rows": current["row_count"],
            "incoming_rows": incoming["row_count"],
            "unchanged_missing_records": len(set(now) - set(next_rows)),
            "eligible_count": sum(item["eligible"] for item in changes),
            "held_count": sum(not item["eligible"] for item in changes),
            "reason_counts": {reason: sum(reason in item["reasons"] for item in changes) for reason in REASONS},
            "reason_labels": REASONS}


def make_import_plan(current: dict, incoming: dict, policy: dict, targets: list[dict], *,
                     workspace_version: int | None = None) -> dict:
    review = review_import(current, incoming, policy)
    if not isinstance(targets, list) or not 1 <= len(targets) <= MAX_TARGETS:
        raise ScopeError("Approve 1–1,000 individual eligible cells.")
    eligible = {(item["token"], item["column"]): item for item in review["changes"] if item["eligible"]}
    selected, seen = [], set()
    for target in targets:
        if (not isinstance(target, dict) or set(target) != {"token", "column"}
                or not isinstance(target["token"], str) or not isinstance(target["column"], str)):
            raise ScopeError("Choose exact import cells from the checked review.")
        identity = (target["token"], target["column"])
        if identity in seen or identity not in eligible:
            raise ScopeError("A held, duplicate or unknown cell cannot be approved. Revise the import or its intended fields.")
        seen.add(identity)
        selected.append(eligible[identity])
    record_order = {key_token(row["key"]): number for number, row in enumerate(current["rows"])}
    field_order = {column["name"]: number for number, column in enumerate(current["columns"])}
    selected.sort(key=lambda item: (record_order[item["token"]], field_order[item["column"]]))
    columns = sorted({item["column"] for item in selected})
    scope = {"format": "csv", "contract": IMPORT_CONTRACT, "primary_key": current["primary_key"],
             "cells": [{"key": item["key"], "column": item["column"]} for item in selected]}
    if len(canonical(scope)) > MAX_SCOPE_BYTES:
        raise ScopeError("The exact import selection exceeds its execution limit. Select fewer cells.")
    plan = {"workflow": "preflight", "contract": IMPORT_CONTRACT, "format": "csv",
            "current_sha256": current["file_sha256"], "incoming_sha256": incoming["file_sha256"],
            "workspace_version": workspace_version, "policy": review["policy"], "targets": selected,
            "scope": scope, "columns": columns, "target_count": len(selected),
            "selected_rows": len({item["token"] for item in selected}), "current_rows": current["row_count"],
            "withheld_cells": len(review["changes"]) - len(selected), "unknown_ids": len(review["unknown_ids"])}
    return {**plan, "sha256": sha256_bytes(canonical(plan))}


def rebind_import_plan(current: dict, incoming: dict, plan: dict) -> dict:
    return make_import_plan(current, incoming, plan["policy"],
        [{"token": item["token"], "column": item["column"]} for item in plan["targets"]],
        workspace_version=plan.get("workspace_version"))


def verify_import(current: dict, incoming: dict, approved: dict, preview: dict, plan: dict) -> dict:
    """Derive every expected value outside both file-producing containers."""
    if rebind_import_plan(current, incoming, plan) != plan:
        raise ScopeError("The reviewed import plan changed.")
    now = row_map(current)
    expected = {(item["token"], item["column"]): item["after"] for item in plan["targets"]}
    selected = {token for token, _ in expected}
    names = [current["primary_key"], *[column["name"] for column in current["columns"]
                                       if column["name"] in plan["columns"]]]
    import_rows = [row for row in current["rows"] if key_token(row["key"]) in selected]
    schema_ok = True
    try:
        compatible(current, preview)
    except ScopeError:
        schema_ok = False
    approved_rows, output = row_map(approved), row_map(preview)
    values_ok = set(approved_rows) == selected and all(
        set(row["values"]) == set(names)
        and all(value == expected.get((token, name), now[token]["values"][name])
                for name, value in row["values"].items())
        for token, row in approved_rows.items() if token in now)
    restored = set(output) == set(now) and all(
        output[token]["values"].get(name) == value for (token, name), value in expected.items())
    protected = set(output) == set(now) and all(
        output[token]["values"].get(name) == value
        for token, row in now.items() for name, value in row["values"].items() if (token, name) not in expected)
    checks = [
        ("approved-csv-integrity", "The approved import passed a fresh CSV inspection", approved.get("integrity") and approved.get("format") == "csv"),
        ("approved-columns", "Only the ID and approved fields are included", [column["name"] for column in approved["columns"]] == names),
        ("approved-records", "Only records with approved changes are included", set(approved_rows) == selected),
        ("approved-record-order", "Approved records keep their export order", [row["key"] for row in approved["rows"]] == [row["key"] for row in import_rows]),
        ("exact-import-values", "Every import value matches the exact approved plan", values_ok),
        ("preview-csv-integrity", "The import result passed a fresh CSV inspection", preview.get("integrity") and preview.get("format") == "csv"),
        ("same-schema", "The resulting export keeps every original field", schema_ok),
        ("same-records", "All existing records stay, with no additions or deletions", set(output) == set(now)),
        ("same-record-order", "The resulting export keeps every record in order", [row["key"] for row in preview["rows"]] == [row["key"] for row in current["rows"]]),
        ("approved-cells", "Every approved update has its requested value", restored),
        ("protected-cells", "Every cell outside approval keeps today's value", protected),
        ("input-fingerprints", "Approval is bound to both uploaded files", current["file_sha256"] == plan["current_sha256"] and incoming["file_sha256"] == plan["incoming_sha256"]),
    ]
    return {"ok": all(bool(ok) for _, _, ok in checks),
            "checks": [{"id": name, "label": label, "ok": bool(ok)} for name, label, ok in checks],
            "diff": plan["targets"], "updated_cells": len(expected), "retained_rows": current["row_count"],
            "withheld_cells": plan["withheld_cells"], "unknown_ids": plan["unknown_ids"]}
