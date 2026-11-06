import copy
import json
import unittest
from rewind.contract import ScopeError, compare, key_token, make_plan, verify_candidate
from rewind.provider import ProviderError, parse_candidate
from rewind.evidence import canonical, sha256_bytes


def integer(value):
    return {"type": "integer", "value": str(value)}


def text(value):
    return {"type": "text", "value": value}


def snapshots():
    backup = {"table": "invoices", "primary_key": "invoice_id", "schema": [["table", "invoices", "invoices", "ordinary schema"]],
              "columns": [{"name": "invoice_id"}, {"name": "price_cents"}, {"name": "payment_status"}], "integrity": True,
              "sequence": [], "file_sha256": "b" * 64, "rows": []}
    for value in range(1, 121):
        backup["rows"].append({"key": integer(value), "values": {"invoice_id": integer(value), "price_cents": integer(value * 125), "payment_status": text("unpaid")}})
    backup["row_count"] = 120
    current = copy.deepcopy(backup)
    current["file_sha256"] = "c" * 64
    for row in current["rows"]:
        value = int(row["key"]["value"])
        row["values"]["price_cents"] = integer(value * 12500)
        if value <= 20:
            row["values"]["payment_status"] = text("paid")
    for value in range(121, 128):
        current["rows"].append({"key": integer(value), "values": {"invoice_id": integer(value), "price_cents": integer(value * 125), "payment_status": text("unpaid")}})
    current["row_count"] = 127
    return backup, current


def plan_for(backup, current):
    choices = compare(backup, current, ["price_cents"])["choices"]
    return make_plan(backup, current, ["price_cents"], [row["token"] for row in choices])


class ContractTests(unittest.TestCase):
    def test_restores_120_prices_keeps_7_new_rows_and_20_paid_updates(self):
        backup, current = snapshots()
        plan = plan_for(backup, current)
        candidate = copy.deepcopy(current)
        for before, after in zip(backup["rows"], candidate["rows"]):
            after["values"]["price_cents"] = before["values"]["price_cents"]
        result = verify_candidate(backup, current, candidate, plan)
        self.assertTrue(result["ok"])
        self.assertEqual((result["restored_cells"], result["retained_rows"], result["retained_new_rows"], result["protected_update_count"]), (120, 127, 7, 20))
        self.assertEqual(plan["reference"]["lost_current_rows"], 7)
        self.assertEqual(plan["reference"]["reverted_protected_updates"], 20)

    def test_full_backup_is_rejected_even_when_all_prices_match(self):
        backup, current = snapshots()
        result = verify_candidate(backup, current, backup, plan_for(backup, current))
        self.assertFalse(result["ok"])
        self.assertEqual(result["failures"]["missing_rows"], 7)
        failed = {item["id"] for item in result["checks"] if not item["ok"]}
        self.assertTrue({"same-records", "protected-cells"}.issubset(failed))

    def test_correct_prices_do_not_authorize_payment_edits(self):
        backup, current = snapshots()
        candidate = copy.deepcopy(current)
        for before, after in zip(backup["rows"], candidate["rows"]):
            after["values"]["price_cents"] = before["values"]["price_cents"]
        candidate["rows"][0]["values"]["payment_status"] = text("unpaid")
        result = verify_candidate(backup, current, candidate, plan_for(backup, current))
        self.assertFalse(result["ok"])
        self.assertEqual(result["failures"]["protected_cells"][0]["column"], "payment_status")

    def test_unselected_changed_price_is_protected(self):
        backup, current = snapshots()
        plan = make_plan(backup, current, ["price_cents"], [key_token(integer(1))])
        candidate = copy.deepcopy(current)
        candidate["rows"][0]["values"]["price_cents"] = backup["rows"][0]["values"]["price_cents"]
        self.assertTrue(verify_candidate(backup, current, candidate, plan)["ok"])
        candidate["rows"][1]["values"]["price_cents"] = backup["rows"][1]["values"]["price_cents"]
        self.assertFalse(verify_candidate(backup, current, candidate, plan)["ok"])

    def test_primary_keys_new_rows_duplicates_and_unknown_columns_are_blocked(self):
        backup, current = snapshots()
        for columns, keys in [(["invoice_id"], [key_token(integer(1))]), (["unknown"], [key_token(integer(1))]),
                              (["price_cents"], [key_token(integer(121))]), (["price_cents"], [key_token(integer(1))] * 2)]:
            with self.assertRaises(ScopeError):
                make_plan(backup, current, columns, keys)

    def test_schema_and_sequence_changes_are_rejected(self):
        backup, current = snapshots()
        candidate = copy.deepcopy(current)
        candidate["schema"][0][3] += " changed"
        candidate["sequence"] = [[text("invoices"), integer(1)]]
        result = verify_candidate(backup, current, candidate, plan_for(backup, current))
        failed = {check["id"] for check in result["checks"] if not check["ok"]}
        self.assertTrue({"same-schema", "same-sequences"}.issubset(failed))

    def test_different_backup_schema_is_not_silently_accepted(self):
        backup, current = snapshots()
        backup["schema"][0][3] = "different"
        with self.assertRaises(ScopeError):
            plan_for(backup, current)

    def test_exact_integer_keys_survive_above_javascript_precision(self):
        self.assertNotEqual(key_token(integer(9007199254740992)), key_token(integer(9007199254740993)))
        self.assertNotEqual(key_token(integer(1)), key_token(text("1")))

    def test_source_is_parsed_without_executing_it_on_the_host(self):
        source = "raise RuntimeError('must never run on host')\ndef recover(a,b,c,d): pass\n"
        result = parse_candidate(json.dumps({"code": source, "explanation": "Fixture for parser only."}))
        self.assertEqual(result["sha256"], sha256_bytes(source.encode()))
        with self.assertRaises(ProviderError):
            parse_candidate(json.dumps({"code": "pass", "explanation": "No recovery function."}))

    def test_plan_fingerprint_binds_scope_and_sources(self):
        backup, current = snapshots()
        plan = plan_for(backup, current)
        raw = {key: value for key, value in plan.items() if key != "sha256"}
        self.assertEqual(plan["sha256"], sha256_bytes(canonical(raw)))
        raw["current_sha256"] = "d" * 64
        self.assertNotEqual(plan["sha256"], sha256_bytes(canonical(raw)))


if __name__ == "__main__":
    unittest.main()
