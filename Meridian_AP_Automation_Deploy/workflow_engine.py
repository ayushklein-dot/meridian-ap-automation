"""Deterministic, auditable four-agent invoice-to-pay workflow for coursework.

This is an explicit graph-style state machine. It intentionally avoids pretending
that a real LLM, mailbox, ERP, tax portal or bank is connected.
"""
from __future__ import annotations
from copy import deepcopy
from datetime import date, datetime
import math
import re
from typing import Any

CONFIDENCE_THRESHOLD = 0.90
HIGH_VALUE_THRESHOLD = 500_000.0
MATCH_TOLERANCE = 0.02
MAX_QUERY_ATTEMPTS = 2


def finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        x = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return x if math.isfinite(x) else None


def gstin_format_valid(value: Any) -> bool:
    return bool(re.fullmatch(r"[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]", str(value or "").strip().upper()))


def _stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def event(state: dict, agent: str, message: str, stage: int) -> None:
    state.setdefault("trace", []).append({"timestamp": _stamp(), "agent": agent, "stage": stage, "message": message})


def _po_grn_match(state: dict) -> tuple[bool, str]:
    if not str(state.get("po") or "").strip():
        return False, "Missing PO reference"
    if not str(state.get("grn") or "").strip():
        return False, "Missing GRN; query warehouse/requester"
    inv = finite_number(state.get("amount"))
    po = finite_number(state.get("po_amount"))
    grn = finite_number(state.get("grn_amount"))
    if any(v is None or v <= 0 for v in (inv, po, grn)):
        return False, "Invoice, PO and GRN amounts must be finite positive numbers"
    for label, ref in (("PO", po), ("GRN", grn)):
        variance = abs(inv-ref)/ref
        if variance > MATCH_TOLERANCE:
            return False, f"Invoice-to-{label} variance {variance:.2%} exceeds ±2%"
    return True, "Invoice matches entered PO and GRN amounts within ±2%"


def intake_agent(state: dict) -> dict:
    state = deepcopy(state)
    state.setdefault("trace", [])
    state.setdefault("query_attempts", 0)
    state.setdefault("messages", [])
    state.setdefault("status", "Processing")
    state.setdefault("human_touchpoints", 0)
    event(state, "Intake Agent", "Created case record and normalized supplied synthetic fields", 1)
    state["stage"] = "Intake and data extraction"
    confidence = finite_number(state.get("scan_conf"))
    if confidence is None or confidence < 0 or confidence > 1:
        state.update(status="Exception — invalid extraction confidence", exception="Confidence must be between 0 and 1.", next_node="END")
        event(state, "Intake Agent", state["exception"], 2)
    elif confidence < CONFIDENCE_THRESHOLD:
        state.update(status="Exception — low-confidence extraction", exception="Confidence below 90%; re-extraction/review required.", next_node="END")
        event(state, "Intake Agent", state["exception"], 2)
    else:
        event(state, "Intake Agent", f"Extraction confidence {confidence:.0%} meets the 90% threshold", 2)
        state["next_node"] = "validation"
    return state


def validation_agent(state: dict, existing_cases: list[dict] | None = None) -> dict:
    state = deepcopy(state)
    existing_cases = existing_cases or []
    state["stage"] = "Validation and three-way match"
    if state.get("next_node") == "END":
        return state
    if state.get("bank_changed"):
        state.update(status="Blocked — bank change", exception="Vendor bank change requires independent human call-back; payment held.", next_node="END")
        event(state, "Validation Agent", state["exception"], 3); return state
    if state.get("duplicate"):
        state.update(status="Blocked — duplicate", exception="Duplicate flag detected; payment held.", next_node="END")
        event(state, "Validation Agent", state["exception"], 3); return state
    invoice_no = str(state.get("invoice_no") or "").strip().casefold()
    vendor = str(state.get("vendor") or "").strip().casefold()
    for other in existing_cases:
        if str(other.get("case_id")) == str(state.get("case_id")):
            continue
        if invoice_no and str(other.get("invoice_no") or "").strip().casefold() == invoice_no:
            if str(other.get("vendor") or "").strip().casefold() == vendor:
                state.update(status="Blocked — duplicate", exception=f"Exact duplicate of {other.get('case_id')}; payment held.", next_node="END")
                event(state, "Validation Agent", state["exception"], 3); return state
            state.update(status="Blocked — possible duplicate", exception=f"Invoice number also appears under another vendor ({other.get('case_id')}); human review required.", next_node="END")
            event(state, "Validation Agent", state["exception"], 3); return state
    if not gstin_format_valid(state.get("gstin")):
        state.update(status="Exception — GSTIN format", exception="GSTIN format check failed; no live registration lookup is performed.", next_node="END")
        event(state, "Validation Agent", state["exception"], 3); return state
    match_ok, match_msg = _po_grn_match(state)
    if not match_ok:
        state["match_reason"] = match_msg
        state["status"] = "Exception — PO/GRN mismatch"
        state["exception"] = match_msg
        if int(state.get("query_attempts", 0)) < MAX_QUERY_ATTEMPTS and state.get("allow_auto_query", False):
            state["next_node"] = "exception"
            event(state, "Validation Agent", f"Mismatch diagnosed: {match_msg}; route to exception resolution", 4)
        else:
            state["next_node"] = "END"
            event(state, "Validation Agent", f"Mismatch held: {match_msg}", 4)
        return state
    event(state, "Validation Agent", match_msg, 4)
    amount = finite_number(state.get("amount"))
    if amount is None or amount <= 0:
        state.update(status="Exception — invalid amount", exception="Invoice amount must be finite and greater than zero.", next_node="END")
        event(state, "Validation Agent", state["exception"], 3); return state
    state["next_node"] = "approval"
    state["status"] = "Ready for approval" if amount <= HIGH_VALUE_THRESHOLD else "Pending CFO approval"
    state["exception"] = "" if amount <= HIGH_VALUE_THRESHOLD else f"Amount exceeds ₹{HIGH_VALUE_THRESHOLD:,.0f}; CFO approval required."
    event(state, "Validation Agent", "Validation passed; routing to approval matrix", 5)
    return state


def exception_agent(state: dict, simulated_reply: dict | None = None) -> dict:
    state = deepcopy(state)
    state["stage"] = "Exception resolution"
    if state.get("next_node") != "exception":
        return state
    attempts = int(state.get("query_attempts", 0))
    if attempts >= MAX_QUERY_ATTEMPTS:
        state.update(status="Escalated — exception unresolved", next_node="END")
        event(state, "Validation Agent", "Maximum two query attempts reached; escalated to human", 5)
        return state
    attempts += 1
    state["query_attempts"] = attempts
    reply = simulated_reply or {}
    state.setdefault("messages", []).append({"attempt": attempts, "to": reply.get("to", "requester/warehouse"), "query": state.get("match_reason", "Please confirm invoice details"), "reply": reply.get("reply", "No response in synthetic inbox"), "timestamp": _stamp()})
    event(state, "Validation Agent", f"Sent synthetic query attempt {attempts} to {reply.get('to', 'requester/warehouse')}", 5)
    if reply.get("resolved"):
        for field in ("po", "po_amount", "grn", "grn_amount", "amount"):
            if field in reply:
                state[field] = reply[field]
        ok, message = _po_grn_match(state)
        if ok:
            state.update(status="Ready for approval", exception="", match_reason=message, next_node="approval")
            event(state, "Validation Agent", "Exception resolved and PO/GRN re-matched successfully", 5)
            return state
        state["match_reason"] = message
    if state["query_attempts"] >= MAX_QUERY_ATTEMPTS:
        state.update(status="Escalated — exception unresolved", exception=f"Not resolved after {MAX_QUERY_ATTEMPTS} query attempts: {state.get('match_reason', 'mismatch')}", next_node="END", human_touchpoints=int(state.get("human_touchpoints", 0))+1)
        event(state, "Validation Agent", state["exception"], 5)
    else:
        state.update(status="Exception — awaiting query response", next_node="exception")
        event(state, "Validation Agent", "Awaiting simulated reply; one query attempt remains", 5)
    return state


def approval_agent(state: dict, approver_role: str | None = None, approve: bool = False) -> dict:
    state = deepcopy(state)
    state["stage"] = "Approval routing"
    if state.get("next_node") not in {"approval", "human_approval"} and state.get("status") not in {"Ready for approval", "Pending CFO approval"}:
        return state
    amount = finite_number(state.get("amount"))
    required_role = "CFO approver" if amount is not None and amount > HIGH_VALUE_THRESHOLD else "AP approver"
    state["required_approver"] = required_role
    if not approve and required_role == "AP approver":
        # Proposal autonomy: clean low-value invoices may auto-approve, while
        # final payment-plan release remains a separate human control.
        if state.get("bank_changed") or state.get("duplicate") or state.get("exception"):
            state.update(status="Approval blocked — validation hold", next_node="END")
            event(state, "Approval Agent", "Auto-approval denied because a validation hold remains", 6)
            return state
        state.update(status="Approved for payment", exception="", next_node="payment", auto_approved=True)
        event(state, "Approval Agent", "Low-value matched invoice auto-approved under the demo matrix; final payment release still requires human plan approval", 6)
        return state
    if not approve:
        state["next_node"] = "human_approval"
        state["status"] = "Pending CFO approval"
        event(state, "Approval Agent", f"Routed to {required_role}; human decision required", 6)
        return state
    state["human_touchpoints"] = int(state.get("human_touchpoints", 0))+1
    if approver_role != required_role:
        state.update(status="Approval blocked — wrong role", exception=f"Required role: {required_role}.", next_node="human_approval")
        event(state, "Approval Agent", state["exception"], 6); return state
    routing_note = (state.get("status") == "Pending CFO approval" and required_role == "CFO approver" and "CFO" in str(state.get("exception", "")).upper())
    if state.get("bank_changed") or state.get("duplicate") or (state.get("exception") and not routing_note):
        state.update(status="Approval blocked — validation hold", next_node="END")
        event(state, "Approval Agent", "Cannot approve while a duplicate, bank change or unresolved exception remains", 6); return state
    state.update(status="Approved for payment", exception="", next_node="payment")
    event(state, "Approval Agent", f"{approver_role} approved invoice; payment release still requires a separate human plan approval", 6)
    return state


def payment_reconciliation_agent(state: dict, cash_limit: float | None = None) -> dict:
    state = deepcopy(state)
    state["stage"] = "Payment scheduling, posting and reconciliation"
    if state.get("status") != "Approved for payment":
        state["next_node"] = "END"
        event(state, "Payment & Reconciliation Agent", "Not eligible: invoice approval missing", 7); return state
    amount = finite_number(state.get("amount"))
    cash = finite_number(cash_limit)
    if amount is None or cash is None or amount > cash:
        state.update(status="Approved — deferred by cash limit", next_node="END")
        event(state, "Payment & Reconciliation Agent", "Invoice approved but deferred; cash limit insufficient", 7); return state
    state["payment_stub"] = {"reference": f"SIM-{state.get('case_id','CASE')}", "amount": amount, "status": "Planned only; not released"}
    state["posting_stub"] = {"journal_reference": f"JRN-{state.get('case_id','CASE')}", "status": "Simulated only; no ERP posting"}
    state["reconciliation_status"] = "Pending simulated bank statement match"
    state.update(status="Payment planned — awaiting release approval", next_node="END")
    event(state, "Payment & Reconciliation Agent", "Created payment/posting stubs; no external transaction executed", 7)
    return state


def run_graph(invoice: dict, existing_cases: list[dict] | None = None, *, approver_role: str | None = None, approve: bool = False, cash_limit: float | None = None, simulated_reply: dict | None = None) -> dict:
    """Run explicit conditional state graph. Human actions are opt-in parameters."""
    state = intake_agent(invoice)
    if state.get("next_node") == "END":
        return state
    state = validation_agent(state, existing_cases)
    if state.get("next_node") == "exception":
        state = exception_agent(state, simulated_reply)
        if state.get("next_node") == "exception":
            return state
        if state.get("next_node") == "END":
            return state
    if state.get("next_node") in {"approval", "human_approval"}:
        state = approval_agent(state, approver_role, approve)
    if state.get("next_node") == "payment":
        state = payment_reconciliation_agent(state, cash_limit)
    state["graph_completed_at"] = _stamp()
    return state


def optimize_payment_run(cases: list[dict], cash_limit: float, today: date | None = None) -> dict:
    """Exact subset search for up to 24 eligible cases, maximizing discount + penalty avoided.

    Benefit is an illustrative objective: early discount value plus an assumed avoided-late
    penalty proxy (0.5% of invoice amount if due today/overdue). Never represents actual terms.
    """
    today = today or date.today()
    cash = finite_number(cash_limit)
    if cash is None or cash < 0:
        raise ValueError("Cash limit must be a finite non-negative amount")
    eligible = []
    for c in cases:
        if c.get("status") != "Approved for payment" or c.get("bank_changed") or c.get("duplicate") or c.get("exception"):
            continue
        amount = finite_number(c.get("amount")); discount = finite_number(c.get("discount_pct", 0))
        if amount is None or amount <= 0 or discount is None or not 0 <= discount <= 100:
            continue
        try: due = date.fromisoformat(str(c.get("due_date")))
        except (TypeError, ValueError): continue
        discount_value = amount * discount / 100
        penalty_proxy = amount * 0.005 if due <= today else 0.0
        benefit = discount_value + penalty_proxy
        eligible.append({"case":c,"amount":amount,"benefit":benefit,"discount_value":discount_value,"penalty_proxy":penalty_proxy,"due":due})
    if len(eligible) > 20:
        eligible = sorted(eligible, key=lambda x: (x["benefit"]/x["amount"], x["benefit"]), reverse=True)[:20]
    # Enumerate all subsets, deterministic tie-break: larger benefit, then lower total, then IDs.
    best_mask, best_benefit, best_total, best_ids = 0, 0.0, 0.0, ()
    n = len(eligible)
    amounts = [x["amount"] for x in eligible]; benefits = [x["benefit"] for x in eligible]
    for mask in range(1 << n):
        total = 0.0
        benefit = 0.0
        ids = []
        for i in range(n):
            if mask & (1 << i):
                total += amounts[i]
                benefit += benefits[i]
                ids.append(str(eligible[i]["case"].get("case_id", "")))
        ids_tuple = tuple(sorted(ids))
        better = benefit > best_benefit + 1e-9
        tied_benefit = math.isclose(benefit, best_benefit, abs_tol=1e-9)
        better_tie = tied_benefit and (total < best_total - 1e-9 or (math.isclose(total, best_total, abs_tol=1e-9) and (not best_ids or ids_tuple < best_ids)))
        if total <= cash + 1e-9 and (better or better_tie):
            best_mask, best_benefit, best_total, best_ids = mask, benefit, total, ids_tuple
    chosen = [eligible[i] for i in range(n) if best_mask & (1 << i)]
    return {"cases":[x["case"]["case_id"] for x in chosen], "total":round(best_total,2), "cash":round(cash,2), "benefit":round(best_benefit,2), "discount_value":round(sum(x["discount_value"] for x in chosen),2), "penalty_avoidance_proxy":round(sum(x["penalty_proxy"] for x in chosen),2), "objective":"maximize illustrative discount value + 0.5% due/overdue penalty proxy", "eligible_count":len(eligible), "optimizer":"exact subset enumeration"}


def reconcile_payment(payment: dict, bank_rows: list[dict]) -> dict:
    """Match a synthetic payment stub to statement rows by exact reference and amount."""
    ref = str(payment.get("reference") or "").strip()
    amount = finite_number(payment.get("amount"))
    if not ref or amount is None or amount <= 0:
        return {"status":"Exception", "reason":"Invalid payment reference or amount"}
    matches = []
    for row in bank_rows:
        row_ref = str(row.get("reference") or row.get("Reference") or "").strip()
        row_amount = finite_number(row.get("amount", row.get("Amount")))
        if row_ref == ref and row_amount is not None and math.isclose(row_amount, amount, abs_tol=0.005):
            matches.append(row)
    if len(matches) == 1:
        return {"status":"Matched", "reason":"One exact reference-and-amount match", "row":matches[0]}
    if len(matches) > 1:
        return {"status":"Exception", "reason":"Multiple statement rows match; manual review required"}
    return {"status":"Unmatched", "reason":"No exact reference-and-amount match"}


def proposal_test_set() -> list[dict]:
    """Create 20 synthetic cases with the proposal's documented mix of scenarios."""
    rows=[]
    def add(name, amount=10000, po=True, grn=True, bank=False, duplicate=False, conf=.98, variance=0, high=False, gst=True, due_offset=10, discount=1.5, expected="STP", exception_kind=""):
        idx=len(rows)+1; cid=f"TEST-{idx:02d}"
        po_amount=amount*(1+variance) if po else None; grn_amount=amount if grn else None
        rows.append({"case_id":cid,"vendor":f"Synthetic Vendor {idx:02d}","invoice_no":f"SYN-{idx:04d}","invoice_date":str(date.today()),"amount":amount,"po":f"PO-{idx:04d}" if po else "","po_amount":po_amount,"grn":f"GRN-{idx:04d}" if grn else "","grn_amount":grn_amount,"gstin":"29ABCDE1234F1Z5" if gst else "INVALID","gst_rate":18,"tds_rate":2,"due_date":str(date.fromordinal(date.today().toordinal()+due_offset)),"discount_pct":discount,"bank_changed":bank,"duplicate":duplicate,"scan_conf":conf,"status":"New","stage":"Intake","exception":"","query_attempts":0,"history":[],"scenario":name,"expected_outcome":expected,"exception_kind":exception_kind,"allow_auto_query":exception_kind in {"price mismatch","missing GRN"}})
    for i in range(6): add(f"Clean invoice {i+1}")
    for i in range(2): add(f"Variance within 2% {i+1}", variance=0.015)
    for i in range(2): add(f"Price mismatch {i+1}", variance=0.08, expected="QUERY → RESOLVE → STP", exception_kind="price mismatch")
    for i in range(2): add(f"Missing GRN {i+1}", grn=False, expected="QUERY → RESOLVE → STP", exception_kind="missing GRN")
    for i in range(2): add(f"Duplicate invoice {i+1}", duplicate=True, expected="BLOCK", exception_kind="duplicate")
    add("Bank detail change", bank=True, expected="HOLD", exception_kind="bank change")
    add("Missing PO", po=False, expected="ESCALATE", exception_kind="missing PO")
    add("High-value invoice", amount=650000, high=True, expected="CFO APPROVAL", exception_kind="high value")
    add("Ambiguous GST/TDS", gst=False, expected="HUMAN REVIEW", exception_kind="tax ambiguity")
    add("Low-quality scan", conf=.72, expected="RE-EXTRACT/REVIEW", exception_kind="low confidence")
    add("Cash shortfall what-if", amount=250000, expected="REPLAN", exception_kind="cash shortfall")
    return rows
