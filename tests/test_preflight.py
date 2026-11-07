import copy
import unittest
from rewind.contract import ScopeError, key_token
from rewind.evidence import canonical, sha256_bytes
from rewind.preflight import REQUIRED_CHECKS, make_import_plan, rebind_import_plan, review_import, verify_import


def snapshot(names, rows, *, raw=None):
    return {"format": "csv", "file_sha256": sha256_bytes(raw if raw is not None else canonical(rows)),
            "table": "records", "primary_key": "id", "schema": [["csv", *names]],
            "columns": [{"name": name, "declared_type": "TEXT", "not_null": False, "default": None,
                         "primary_key": name == "id"} for name in names],
            "row_count": len(rows), "integrity": True, "sequence": [],
            "rows": [{"key": {"type": "text", "value": row[names.index("id")]},
                      "values": {name: {"type": "text", "value": value} for name, value in zip(names, row)}} for row in rows]}


def versions():
    names = ["id", "price", "paid", "note"]
    current = snapshot(names, [["0001", "10.00", "yes", "first"], ["0002", "20.00", "yes", "second"],
        ["0003", "30.00", "yes", "third"], ["0004", "40.00", "yes", "fourth"], ["0005", "50.00", "yes", "keep"]])
    incoming = snapshot(names, [["0001", "11.00", "no", "first"], ["0002", "2000.00", "no", "second"],
        ["0003", "", "no", "third"], ["0004", "44.00", "no", "fourth"], ["9999", "60.00", "no", "unknown"]])
    return current, incoming


POLICY = {"columns": ["price"], "numeric_column": "price", "max_percent": 25}


def plan_for(current, incoming, policy=POLICY):
    review = review_import(current, incoming, policy)
    return make_import_plan(current, incoming, policy,
        [{"token": item["token"], "column": item["column"]} for item in review["changes"] if item["eligible"]], workspace_version=7)


def artifacts(current, plan):
    preview = copy.deepcopy(current)
    updates = {(item["token"], item["column"]): item["after"] for item in plan["targets"]}
    selected = {token for token, _ in updates}
    for row in preview["rows"]:
        for name in row["values"]:
            if (key_token(row["key"]), name) in updates:
                row["values"][name] = copy.deepcopy(updates[(key_token(row["key"]), name)])
    names = ["id", *[item["name"] for item in current["columns"] if item["name"] in plan["columns"]]]
    approved = snapshot(names, [[row["values"][name]["value"] for name in names]
        for row in preview["rows"] if key_token(row["key"]) in selected])
    return approved, preview


class ImportContractTests(unittest.TestCase):
    def test_price_update_holds_damage_and_preserves_paid_and_missing_records(self):
        current, incoming = versions()
        review = review_import(current, incoming, POLICY)
        self.assertEqual((review["eligible_count"], review["held_count"], len(review["unknown_ids"])), (2, 6, 1))
        self.assertEqual(review["unchanged_missing_records"], 1)
        self.assertEqual(review["reason_counts"]["unintended-field"], 4)
        self.assertEqual(review["reason_counts"]["blank-overwrite"], 1)
        self.assertEqual(review["reason_counts"]["large-numeric-change"], 1)
        plan = plan_for(current, incoming)
        approved, preview = artifacts(current, plan)
        result = verify_import(current, incoming, approved, preview, plan)
        self.assertTrue(result["ok"])
        self.assertEqual({item["id"] for item in result["checks"]}, REQUIRED_CHECKS)
        self.assertEqual((result["updated_cells"], result["retained_rows"]), (2, 5))
        self.assertEqual([row["values"]["paid"]["value"] for row in preview["rows"]], ["yes"] * 5)
        self.assertEqual([row["values"]["price"]["value"] for row in preview["rows"]], ["11.00", "20.00", "30.00", "44.00", "50.00"])
        self.assertEqual([column["name"] for column in approved["columns"]], ["id", "price"])

    def test_partial_import_preserves_omitted_fields_and_exact_id_strings(self):
        current, _ = versions()
        incoming = snapshot(["id", "price"], [["0001", "11.00"], ["1", "11.00"]])
        review = review_import(current, incoming, POLICY)
        self.assertEqual(review["eligible_count"], 1)
        self.assertEqual(review["unknown_ids"][0]["key"]["value"], "1")
        plan = plan_for(current, incoming)
        self.assertTrue(verify_import(current, incoming, *artifacts(current, plan), plan)["ok"])

    def test_blank_and_whitespace_overwrites_are_held(self):
        current = snapshot(["id", "note"], [["01", "keep"], ["02", "keep"]])
        incoming = snapshot(["id", "note"], [["01", ""], ["02", " \t "]])
        review = review_import(current, incoming, {**POLICY, "columns": ["note"], "numeric_column": ""})
        self.assertEqual(review["eligible_count"], 0)
        self.assertTrue(all("blank-overwrite" in item["reasons"] for item in review["changes"]))

    def test_numeric_boundary_uses_exact_decimal_arithmetic(self):
        current = snapshot(["id", "price"], [["01", "1.0000000000000000"], ["02", "1.0000000000000000"]])
        incoming = snapshot(["id", "price"], [["01", "1.2500000000000000"], ["02", "1.2500000000000001"]])
        review = review_import(current, incoming, POLICY)
        self.assertTrue(review["changes"][0]["eligible"])
        self.assertIn("large-numeric-change", review["changes"][1]["reasons"])

    def test_unsupported_numeric_formats_and_zero_baselines_are_held(self):
        current = snapshot(["id", "price"], [["01", "0"], ["02", "10"], ["03", "10"], ["04", "10"]])
        incoming = snapshot(["id", "price"], [["01", "1"], ["02", "$11"], ["03", "1e999999999"], ["04", "NaN"]])
        review = review_import(current, incoming, POLICY)
        self.assertEqual(review["eligible_count"], 0)
        self.assertIn("zero-baseline", review["changes"][0]["reasons"])
        self.assertEqual(review["reason_counts"]["numeric-format"], 3)

    def test_formulas_and_unicode_are_literal_without_numeric_guard(self):
        current = snapshot(["id", "note"], [["0001", "old"], ["0002", "old"]])
        incoming = snapshot(["id", "note"], [["0001", "=SUM(1,2)"], ["0002", "  café, 漢字\nline two  "]])
        policy = {**POLICY, "columns": ["note"], "numeric_column": ""}
        plan = plan_for(current, incoming, policy)
        approved, preview = artifacts(current, plan)
        self.assertTrue(verify_import(current, incoming, approved, preview, plan)["ok"])
        self.assertEqual(preview["rows"][1]["values"]["note"]["value"], "  café, 漢字\nline two  ")

    def test_policy_and_schema_cannot_authorize_unknown_fields_or_keys(self):
        current, incoming = versions()
        invalid = [{**POLICY, "columns": ["id"]}, {**POLICY, "columns": ["missing"]}, {**POLICY, "columns": []},
            {**POLICY, "columns": ["price", "price"]}, {**POLICY, "max_percent": True}, {**POLICY, "max_percent": 1001},
            {**POLICY, "numeric_column": "paid"}, {**POLICY, "override": True}]
        for policy in invalid:
            with self.assertRaises(ScopeError): review_import(current, incoming, policy)
        with self.assertRaises(ScopeError): review_import(current, snapshot(["id", "secret"], [["0001", "new"]]), POLICY)

    def test_held_unknown_and_duplicate_cells_cannot_be_approved(self):
        current, incoming = versions()
        review = review_import(current, incoming, POLICY)
        for item in [entry for entry in review["changes"] if not entry["eligible"]]:
            with self.assertRaises(ScopeError):
                make_import_plan(current, incoming, POLICY, [{"token": item["token"], "column": item["column"]}])
        good = next(item for item in review["changes"] if item["eligible"])
        target = {"token": good["token"], "column": good["column"]}
        with self.assertRaises(ScopeError): make_import_plan(current, incoming, POLICY, [target, target])
        with self.assertRaises(ScopeError): make_import_plan(current, incoming, POLICY, [{"token": review["unknown_ids"][0]["token"], "column": "price"}])

    def test_approval_binds_policy_exact_cells_input_hashes_and_version(self):
        current, incoming = versions()
        plan = plan_for(current, incoming)
        self.assertEqual(rebind_import_plan(current, incoming, plan), plan)
        changed = copy.deepcopy(plan); changed["policy"]["max_percent"] = 30
        self.assertNotEqual(rebind_import_plan(current, incoming, changed)["sha256"], plan["sha256"])
        changed = copy.deepcopy(current); changed["file_sha256"] = "a" * 64
        self.assertNotEqual(rebind_import_plan(changed, incoming, plan)["sha256"], plan["sha256"])
        changed = copy.deepcopy(plan); changed["workspace_version"] += 1
        self.assertNotEqual(rebind_import_plan(current, incoming, changed)["sha256"], plan["sha256"])
        changed = copy.deepcopy(plan); changed["targets"][0]["after"]["value"] = "9999"
        with self.assertRaises(ScopeError): verify_import(current, incoming, *artifacts(current, plan), changed)

    def test_cross_product_of_keys_and_fields_is_rejected(self):
        current, incoming = versions()
        policy = {**POLICY, "columns": ["price", "paid"], "numeric_column": ""}
        review = review_import(current, incoming, policy)
        selected = [item for item in review["changes"] if (item["key"]["value"], item["column"]) in {("0001", "price"), ("0004", "paid")}]
        plan = make_import_plan(current, incoming, policy, [{"token": item["token"], "column": item["column"]} for item in selected])
        approved, preview = artifacts(current, plan)
        self.assertTrue(verify_import(current, incoming, approved, preview, plan)["ok"])
        approved["rows"][0]["values"]["paid"]["value"] = "no"
        preview["rows"][0]["values"]["paid"]["value"] = "no"
        result = verify_import(current, incoming, approved, preview, plan)
        self.assertFalse(result["ok"])
        self.assertEqual({item["id"] for item in result["checks"] if not item["ok"]}, {"exact-import-values", "protected-cells"})

    def test_removing_adding_reordering_or_changing_unapproved_values_fails(self):
        current, incoming = versions(); plan = plan_for(current, incoming)
        approved, preview = artifacts(current, plan)
        bad = []
        value = copy.deepcopy(preview); value["rows"].pop(); value["row_count"] -= 1; bad.append(value)
        value = copy.deepcopy(preview); value["rows"].append(copy.deepcopy(incoming["rows"][-1])); value["row_count"] += 1; bad.append(value)
        value = copy.deepcopy(preview); value["rows"].reverse(); bad.append(value)
        value = copy.deepcopy(preview); value["rows"][4]["values"]["note"]["value"] = "lost"; bad.append(value)
        for value in bad: self.assertFalse(verify_import(current, incoming, approved, value, plan)["ok"])


if __name__ == "__main__":
    unittest.main()
