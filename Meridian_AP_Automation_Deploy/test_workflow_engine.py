"""Unit tests for the deterministic four-agent workflow engine."""
import unittest
from datetime import date, timedelta
from workflow_engine import (
    run_graph, proposal_test_set, optimize_payment_run, reconcile_payment,
    intake_agent, validation_agent, exception_agent, approval_agent,
    payment_reconciliation_agent, gstin_format_valid, finite_number,
)


def base_case(**updates):
    c = {
        "case_id":"CASE-1", "vendor":"Demo Supplier", "invoice_no":"INV-1",
        "invoice_date":str(date.today()), "amount":10000, "po":"PO-1", "po_amount":10000,
        "grn":"GRN-1", "grn_amount":10000, "gstin":"29ABCDE1234F1Z5",
        "scan_conf":0.98, "bank_changed":False, "duplicate":False, "due_date":str(date.today()+timedelta(days=3)),
        "discount_pct":2, "status":"New", "exception":"", "query_attempts":0,
    }
    c.update(updates)
    return c


class TestWorkflowEngine(unittest.TestCase):
    def test_proposal_test_set_has_twenty_cases(self):
        self.assertEqual(len(proposal_test_set()), 20)

    def test_intake_records_trace(self):
        result = intake_agent(base_case())
        self.assertEqual(result["next_node"], "validation")
        self.assertEqual(result["trace"][0]["agent"], "Intake Agent")

    def test_low_confidence_stops(self):
        result = run_graph(base_case(scan_conf=0.5))
        self.assertIn("low-confidence", result["status"])
        self.assertEqual(result["next_node"], "END")

    def test_nan_confidence_stops_without_crash(self):
        result = run_graph(base_case(scan_conf=float('nan')))
        self.assertIn("invalid extraction", result["status"])

    def test_bank_change_holds(self):
        result = run_graph(base_case(bank_changed=True))
        self.assertIn("bank change", result["status"])

    def test_duplicate_holds(self):
        result = run_graph(base_case(duplicate=True))
        self.assertIn("duplicate", result["status"])

    def test_gstin_format_only(self):
        self.assertTrue(gstin_format_valid("29ABCDE1234F1Z5"))
        self.assertFalse(gstin_format_valid("INVALID"))

    def test_exact_duplicate_across_existing_cases_holds(self):
        old = base_case(case_id="CASE-OLD")
        result = run_graph(base_case(), [old])
        self.assertIn("duplicate", result["status"])

    def test_same_invoice_other_vendor_suspicious(self):
        old = base_case(case_id="CASE-OLD")
        result = run_graph(base_case(vendor="Other Supplier"), [old])
        self.assertIn("possible duplicate", result["status"])

    def test_po_variance_beyond_two_percent_holds(self):
        result = run_graph(base_case(po_amount=12000))
        self.assertIn("PO/GRN mismatch", result["status"])

    def test_missing_po_holds(self):
        result = run_graph(base_case(po="", po_amount=None))
        self.assertIn("PO/GRN mismatch", result["status"])

    def test_query_resolves_mismatch_and_autoadproves_low_value(self):
        c = base_case(po_amount=12000, allow_auto_query=True)
        reply = {"resolved":True,"po_amount":10000,"to":"vendor","reply":"confirmed"}
        result = run_graph(c, simulated_reply=reply, cash_limit=20000)
        self.assertEqual(result["status"], "Payment planned — awaiting release approval")
        self.assertEqual(result["query_attempts"], 1)
        self.assertTrue(result["auto_approved"])

    def test_failed_query_escalates_after_two_attempts(self):
        c = base_case(po_amount=12000, allow_auto_query=True, query_attempts=0)
        # Execute validation + exception directly to exercise the second attempt cap.
        state = intake_agent(c)
        state = validation_agent(state)
        state["next_node"] = "exception"
        result = exception_agent(state, {"resolved":False,"reply":"No correction"})
        self.assertEqual(result["query_attempts"], 1)
        result["next_node"] = "exception"
        result = exception_agent(result, {"resolved":False,"reply":"Still no correction"})
        self.assertIn("Escalated", result["status"])
        self.assertEqual(result["query_attempts"], 2)

    def test_high_value_requires_cfo(self):
        c = base_case(amount=650000, po_amount=650000, grn_amount=650000)
        result = run_graph(c)
        self.assertEqual(result["status"], "Pending CFO approval")
        approved_wrong = run_graph(c, approver_role="AP approver", approve=True, cash_limit=700000)
        self.assertIn("wrong role", approved_wrong["status"])
        approved = run_graph(c, approver_role="CFO approver", approve=True, cash_limit=700000)
        self.assertEqual(approved["status"], "Payment planned — awaiting release approval")

    def test_payment_agent_defers_when_cash_short(self):
        c = base_case(status="Approved for payment")
        result = payment_reconciliation_agent(c, 100)
        self.assertIn("deferred", result["status"])
        self.assertNotIn("payment_stub", result)

    def test_payment_agent_does_not_run_without_approval(self):
        result = payment_reconciliation_agent(base_case(status="Ready for approval"), 20000)
        self.assertNotIn("payment_stub", result)

    def test_optimizer_obeys_cash_limit(self):
        today = date.today()
        cases = [base_case(case_id="A", amount=100, po_amount=100, grn_amount=100, status="Approved for payment", due_date=str(today), discount_pct=2),
                 base_case(case_id="B", amount=200, po_amount=200, grn_amount=200, status="Approved for payment", due_date=str(today+timedelta(days=10)), discount_pct=1)]
        plan = optimize_payment_run(cases, 200, today)
        self.assertLessEqual(plan["total"], 200)
        self.assertEqual(plan["cases"], ["A"])

    def test_optimizer_rejects_invalid_cash(self):
        with self.assertRaises(ValueError): optimize_payment_run([], float('nan'))

    def test_optimizer_ignores_unapproved_cases(self):
        c = base_case(status="Ready for approval")
        self.assertEqual(optimize_payment_run([c], 100000)["cases"], [])

    def test_reconciliation_exact_match(self):
        result = reconcile_payment({"reference":"SIM-1","amount":100}, [{"reference":"SIM-1","amount":100}])
        self.assertEqual(result["status"], "Matched")

    def test_reconciliation_unmatched(self):
        result = reconcile_payment({"reference":"SIM-1","amount":100}, [{"reference":"SIM-2","amount":100}])
        self.assertEqual(result["status"], "Unmatched")

    def test_reconciliation_duplicate_match_escalates(self):
        rows = [{"reference":"SIM-1","amount":100},{"reference":"SIM-1","amount":100}]
        self.assertEqual(reconcile_payment({"reference":"SIM-1","amount":100}, rows)["status"], "Exception")

    def test_reconciliation_rejects_invalid_amount(self):
        self.assertEqual(reconcile_payment({"reference":"SIM-1","amount":float('inf')}, [])["status"], "Exception")

    def test_finite_number_rejects_bool_nan_and_infinity(self):
        self.assertIsNone(finite_number(True)); self.assertIsNone(finite_number(float('nan'))); self.assertIsNone(finite_number(float('inf')))

    def test_twenty_case_run_hits_proposal_sixty_percent_stp_target(self):
        results=[]
        for c in proposal_test_set():
            reply=None
            if c.get("exception_kind") == "price mismatch": reply={"resolved":True,"to":"vendor","reply":"confirmed","po_amount":c["amount"]}
            elif c.get("exception_kind") == "missing GRN": reply={"resolved":True,"to":"warehouse","reply":"confirmed","grn":f"GRN-CONF-{c['case_id']}","grn_amount":c["amount"]}
            r=run_graph(c, existing_cases=[], simulated_reply=reply, cash_limit=200000)
            stp=bool(r.get("auto_approved")) and r.get("status") in {"Approved for payment", "Payment planned — awaiting release approval"}
            results.append(stp)
        self.assertEqual(sum(results), 12)
        self.assertEqual(sum(results)/20, .60)


    def test_price_mismatch_and_missing_grn_are_labelled_as_resolved_exception_paths(self):
        rows = proposal_test_set()
        for row in rows:
            if row.get("exception_kind") in {"price mismatch", "missing GRN"}:
                self.assertEqual(row["expected_outcome"], "QUERY → RESOLVE → STP")
                reply = None
                if row["exception_kind"] == "price mismatch":
                    reply = {"resolved": True, "to": "vendor", "reply": "confirmed", "po_amount": row["amount"]}
                else:
                    reply = {"resolved": True, "to": "warehouse", "reply": "confirmed",
                             "grn": f"GRN-CONF-{row['case_id']}", "grn_amount": row["amount"]}
                result = run_graph(row, existing_cases=[], simulated_reply=reply, cash_limit=200000)
                self.assertEqual(result["query_attempts"], 1)
                self.assertEqual(result["status"], "Payment planned — awaiting release approval")
                self.assertTrue(result.get("auto_approved"))
                self.assertFalse(result.get("exception"))

if __name__ == "__main__":
    unittest.main(verbosity=2)
