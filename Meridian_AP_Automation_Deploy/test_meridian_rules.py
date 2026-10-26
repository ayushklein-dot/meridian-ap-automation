"""Independent unit tests for core Meridian rules; does not launch Streamlit."""
import ast
import datetime
import json
import math
import re
import unittest
from pathlib import Path
import pandas as pd

APP = Path(__file__).with_name("app.py")
TREE = ast.parse(APP.read_text(encoding="utf-8"))

# Extract only pure rule functions from app.py, avoiding Streamlit UI execution.
FUNCTIONS = {
    "norm", "finite_number", "valid_demo_gstin", "duplicate_match", "possible_duplicate_match",
    "match_po_grn", "payment_eligible", "invoice_approval_required", "invoice_approval_check", "validate_payment_plan"
}
nodes = [node for node in TREE.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in FUNCTIONS]
namespace = {
    "re": re,
    "math": math,
    "datetime": datetime,
    "pd": pd,
    "CONFIDENCE_THRESHOLD": 0.90,
    "PO_GRN_TOLERANCE": 0.02,
    "HIGH_VALUE_THRESHOLD": 500000,
}
exec(compile(ast.Module(body=nodes, type_ignores=[]), str(APP), "exec"), namespace)

valid_demo_gstin = namespace["valid_demo_gstin"]
duplicate_match = namespace["duplicate_match"]
possible_duplicate_match = namespace["possible_duplicate_match"]
match_po_grn = namespace["match_po_grn"]
payment_eligible = namespace["payment_eligible"]
invoice_approval_required = namespace["invoice_approval_required"]
invoice_approval_check = namespace["invoice_approval_check"]
validate_payment_plan = namespace["validate_payment_plan"]

class TestMeridianRules(unittest.TestCase):
    def base_case(self):
        return {
            "case_id": "INV-9001", "vendor": "Demo Vendor", "invoice_no": "D-100",
            "amount": 1000, "po": "PO-1", "po_amount": 1000,
            "grn": "GRN-1", "grn_amount": 1000, "gstin": "29ABCDE1234F1Z5",
            "scan_conf": 0.98, "bank_changed": False, "duplicate": False,
            "status": "Approved for payment", "exception": "", "due_date": "2026-11-01",
        }

    def test_valid_gstin_format(self):
        self.assertTrue(valid_demo_gstin("29ABCDE1234F1Z5"))
        self.assertTrue(valid_demo_gstin("29abcde1234f1z5"))

    def test_invalid_gstin_format(self):
        self.assertFalse(valid_demo_gstin("NOT-A-GSTIN"))
        self.assertFalse(valid_demo_gstin(""))
        self.assertFalse(valid_demo_gstin("29ABCDE1234F1Z"))

    def test_po_grn_exact_match(self):
        ok, _ = match_po_grn(self.base_case())
        self.assertTrue(ok)

    def test_po_grn_within_tolerance(self):
        case = self.base_case()
        case["po_amount"] = 1010
        ok, _ = match_po_grn(case)
        self.assertTrue(ok)

    def test_po_grn_outside_tolerance(self):
        case = self.base_case()
        case["po_amount"] = 1100
        ok, _ = match_po_grn(case)
        self.assertFalse(ok)

    def test_missing_grn_blocks_match(self):
        case = self.base_case()
        case["grn"] = ""
        ok, _ = match_po_grn(case)
        self.assertFalse(ok)

    def test_zero_invoice_amount_blocks_match(self):
        case = self.base_case()
        case["amount"] = 0
        ok, message = match_po_grn(case)
        self.assertFalse(ok)
        self.assertIn("greater than zero", message)

    def test_missing_po_amount_blocks_match(self):
        case = self.base_case()
        case["po_amount"] = None
        ok, _ = match_po_grn(case)
        self.assertFalse(ok)

    def test_negative_grn_amount_blocks_match(self):
        case = self.base_case()
        case["grn_amount"] = -1
        ok, _ = match_po_grn(case)
        self.assertFalse(ok)

    def test_nan_invoice_amount_blocks_match(self):
        case = self.base_case()
        case["amount"] = float("nan")
        self.assertFalse(match_po_grn(case)[0])

    def test_infinite_po_amount_blocks_match(self):
        case = self.base_case()
        case["po_amount"] = float("inf")
        self.assertFalse(match_po_grn(case)[0])

    def test_malformed_po_amount_blocks_match_without_crash(self):
        case = self.base_case()
        case["po_amount"] = "not a number"
        self.assertFalse(match_po_grn(case)[0])

    def test_duplicate_vendor_and_invoice(self):
        existing = [{"case_id":"INV-1","vendor":"Demo Vendor","invoice_no":" d-100 "}]
        self.assertIsNotNone(duplicate_match({"vendor":"demo vendor","invoice_no":"D-100"}, existing))

    def test_different_vendor_same_invoice_is_suspicious(self):
        existing = [{"case_id":"INV-1","vendor":"Other Vendor","invoice_no":"D-100"}]
        self.assertIsNotNone(possible_duplicate_match({"vendor":"Demo Vendor","invoice_no":"D-100"}, existing))

    def test_ready_for_approval_is_not_payment_eligible(self):
        case = self.base_case()
        case["status"] = "Ready for approval"
        self.assertFalse(payment_eligible(case))

    def test_approved_valid_case_is_payment_eligible(self):
        self.assertTrue(payment_eligible(self.base_case()))

    def test_bank_change_blocks_payment_eligibility(self):
        case = self.base_case()
        case["bank_changed"] = True
        self.assertFalse(payment_eligible(case))

    def test_duplicate_blocks_payment_eligibility(self):
        case = self.base_case()
        case["duplicate"] = True
        self.assertFalse(payment_eligible(case))

    def test_exception_blocks_payment_eligibility(self):
        case = self.base_case()
        case["exception"] = "Review required"
        self.assertFalse(payment_eligible(case))


    def test_normal_invoice_can_be_approved_by_ap(self):
        case = self.base_case()
        case["status"] = "Ready for approval"
        ok, _ = invoice_approval_check(case, "AP approver")
        self.assertTrue(ok)

    def test_high_value_invoice_requires_cfo(self):
        case = self.base_case()
        case.update(amount=600000, po_amount=600000, grn_amount=600000,
                    status="Pending CFO approval",
                    exception="Amount exceeds demo threshold; CFO approval required.")
        ok, message = invoice_approval_check(case, "AP approver")
        self.assertFalse(ok)
        self.assertIn("CFO", message)
        ok, _ = invoice_approval_check(case, "CFO approver")
        self.assertTrue(ok)

    def test_unresolved_exception_blocks_approval(self):
        case = self.base_case()
        case["exception"] = "Bank validation failed"
        ok, _ = invoice_approval_check(case, "AP approver")
        self.assertFalse(ok)

    def test_invalid_amount_blocks_approval(self):
        case = self.base_case()
        case["amount"] = 0
        ok, _ = invoice_approval_check(case, "AP approver")
        self.assertFalse(ok)

    def test_invoice_approval_statuses(self):
        self.assertTrue(invoice_approval_required({"status":"Ready for approval"}))
        self.assertTrue(invoice_approval_required({"status":"Pending CFO approval"}))
        self.assertFalse(invoice_approval_required({"status":"Approved for payment"}))


    def test_seeded_cfo_routing_note_is_not_treated_as_unresolved_exception(self):
        case = self.base_case()
        case.update(amount=675000, po_amount=675000, grn_amount=675000,
                    status="Pending CFO approval",
                    exception="Amount exceeds illustrative ₹5,00,000 CFO threshold.")
        ok, message = invoice_approval_check(case, "CFO approver")
        self.assertTrue(ok, message)

    def test_payment_plan_validates_total(self):
        case = self.base_case()
        plan = {"cases": [case["case_id"]], "cash": 2000, "total": 1000}
        ok, _ = validate_payment_plan(plan, [case])
        self.assertTrue(ok)

    def test_payment_plan_rejects_tampered_total(self):
        case = self.base_case()
        plan = {"cases": [case["case_id"]], "cash": 2000, "total": 999}
        ok, message = validate_payment_plan(plan, [case])
        self.assertFalse(ok)
        self.assertIn("does not match", message)

    def test_payment_plan_rejects_duplicate_case_ids(self):
        case = self.base_case()
        plan = {"cases": [case["case_id"], case["case_id"]], "cash": 3000, "total": 2000}
        ok, _ = validate_payment_plan(plan, [case])
        self.assertFalse(ok)

    def test_malformed_confidence_fails_closed(self):
        case = self.base_case()
        case["scan_conf"] = "not-a-number"
        self.assertFalse(payment_eligible(case))

    def test_nan_confidence_fails_closed(self):
        case = self.base_case()
        case["scan_conf"] = float("nan")
        self.assertFalse(payment_eligible(case))

    def test_confidence_above_one_fails_closed(self):
        case = self.base_case()
        case["scan_conf"] = 1.01
        self.assertFalse(payment_eligible(case))

    def test_payment_plan_rejects_unhashable_ids_without_crash(self):
        case = self.base_case()
        plan = {"cases": [[case["case_id"]]], "cash": 2000, "total": 1000}
        ok, _ = validate_payment_plan(plan, [case])
        self.assertFalse(ok)

    def test_payment_plan_rejects_nan_cash(self):
        case = self.base_case()
        plan = {"cases": [case["case_id"]], "cash": float("nan"), "total": 1000}
        ok, _ = validate_payment_plan(plan, [case])
        self.assertFalse(ok)

if __name__ == "__main__":
    unittest.main(verbosity=2)
