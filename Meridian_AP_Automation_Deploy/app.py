from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import re
import sqlite3
import json
import math
import pandas as pd
import streamlit as st
from workflow_engine import (
    run_graph, proposal_test_set, optimize_payment_run, reconcile_payment,
    finite_number as workflow_finite_number,
)

# Meridian AP Automation — coursework prototype.
# Uses synthetic data only. No real OCR/LLM, ERP, email, tax portal, bank,
# accounting-system posting, or payment execution.
st.set_page_config(page_title="Meridian AP Automation", page_icon="🧾", layout="wide")

APP_VERSION = "4.1-coursework"
DB_PATH = Path(__file__).with_name("meridian_demo.sqlite3")
CONFIDENCE_THRESHOLD = 0.90
HIGH_VALUE_THRESHOLD = 500_000
PO_GRN_TOLERANCE = 0.02

SEED_CASES = [
    {"case_id":"INV-1001","vendor":"Apex Fasteners Pvt Ltd","invoice_no":"APX-8841","invoice_date":"2026-10-20","amount":125000,"po":"PO-4501","po_amount":125000,"grn":"GRN-4501","grn_amount":125000,"gstin":"29ABCDE1234F1Z5","gst_rate":18,"tds_rate":2,"due_date":"2026-11-05","discount_pct":2.0,"bank_changed":False,"duplicate":False,"scan_conf":0.98,"status":"Ready for approval","stage":"Approval routing","exception":"","query_attempts":0,"history":["Seeded synthetic invoice; validation checks passed; awaiting invoice approval."]},
    {"case_id":"INV-1002","vendor":"Northstar Industrial Supply","invoice_no":"NS-2291","invoice_date":"2026-10-21","amount":86400,"po":"PO-4502","po_amount":86400,"grn":"GRN-4502","grn_amount":86400,"gstin":"29FGHIJ5678K1Z2","gst_rate":18,"tds_rate":2,"due_date":"2026-10-29","discount_pct":0.0,"bank_changed":True,"duplicate":False,"scan_conf":0.97,"status":"Blocked — human review","stage":"Validation","exception":"Vendor bank details changed; independent call-back confirmation required.","query_attempts":0,"history":["Bank-detail change detected; payment held for human review."]},
    {"case_id":"INV-1003","vendor":"Kaveri Packaging Works","invoice_no":"KPW-1044","invoice_date":"2026-10-18","amount":43200,"po":"PO-4503","po_amount":43200,"grn":"","grn_amount":None,"gstin":"29KLMNO9012P1Z7","gst_rate":12,"tds_rate":1,"due_date":"2026-11-12","discount_pct":1.0,"bank_changed":False,"duplicate":False,"scan_conf":0.82,"status":"Exception — low-confidence extraction","stage":"Data extraction","exception":"Extraction confidence below 90%; re-extraction or review required.","query_attempts":1,"history":["Low extraction confidence; payment held."]},
    {"case_id":"INV-1004","vendor":"Apex Fasteners Pvt Ltd","invoice_no":"APX-8841","invoice_date":"2026-10-20","amount":125000,"po":"PO-4501","po_amount":125000,"grn":"GRN-4501","grn_amount":125000,"gstin":"29ABCDE1234F1Z5","gst_rate":18,"tds_rate":2,"due_date":"2026-11-05","discount_pct":2.0,"bank_changed":False,"duplicate":True,"scan_conf":0.99,"status":"Blocked — duplicate","stage":"Validation","exception":"Duplicate vendor + invoice number detected. Do not pay.","query_attempts":0,"history":["Duplicate check matched INV-1001; payment blocked."]},
    {"case_id":"INV-1005","vendor":"Zenith Machine Tools","invoice_no":"ZMT-7772","invoice_date":"2026-10-22","amount":675000,"po":"PO-4505","po_amount":675000,"grn":"GRN-4505","grn_amount":675000,"gstin":"29QRSTU3456V1Z8","gst_rate":18,"tds_rate":2,"due_date":"2026-10-30","discount_pct":3.0,"bank_changed":False,"duplicate":False,"scan_conf":0.96,"status":"Pending CFO approval","stage":"Approval routing","exception":"Amount exceeds illustrative ₹5,00,000 CFO threshold.","query_attempts":0,"history":["High-value invoice routed for CFO approval."]},
    {"case_id":"INV-1006","vendor":"BluePeak Components","invoice_no":"BPC-6610","invoice_date":"2026-10-22","amount":245000,"po":"PO-4506","po_amount":245000,"grn":"GRN-4506","grn_amount":245000,"gstin":"29UVWXY6789Z1Z4","gst_rate":18,"tds_rate":2,"due_date":"2026-11-02","discount_pct":2.5,"bank_changed":False,"duplicate":False,"scan_conf":0.99,"status":"Ready for approval","stage":"Approval routing","exception":"","query_attempts":0,"history":["Seeded synthetic invoice; validation checks passed; awaiting invoice approval."]},
]

def now_stamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

def money(value) -> str:
    if value is None or pd.isna(value):
        return "—"
    return "₹{:,.2f}".format(float(value))

def norm(value) -> str:
    return str(value or "").strip().casefold()

def finite_number(value) -> float | None:
    """Return a finite float, or None for malformed, NaN, or infinite values."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None

def valid_demo_gstin(gstin: str) -> bool:
    # Format-only check; not a live GST registration check.
    return bool(re.fullmatch(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$", str(gstin or "").strip().upper()))

def log_event(case: dict, message: str) -> None:
    event = {"timestamp": now_stamp(), "case_id": case.get("case_id", ""), "vendor": case.get("vendor", ""), "event": message}
    case.setdefault("history", []).append(f'{event["timestamp"]} — {message}')
    persist_audit(event)

def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS invoices (
        case_id TEXT PRIMARY KEY, data_json TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS audit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
        case_id TEXT, vendor TEXT, event TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS app_state (
        state_key TEXT PRIMARY KEY, state_json TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    conn.commit()
    return conn

def persist_audit(event: dict) -> None:
    conn = db_connect()
    with conn:
        conn.execute(
            "INSERT INTO audit_events(timestamp, case_id, vendor, event) VALUES (?, ?, ?, ?)",
            (event["timestamp"], event.get("case_id", ""), event.get("vendor", ""), event["event"]),
        )
    conn.close()

def save_case(case: dict) -> None:
    conn = db_connect()
    with conn:
        conn.execute(
            "INSERT INTO invoices(case_id, data_json, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(case_id) DO UPDATE SET data_json=excluded.data_json, updated_at=excluded.updated_at",
            (case["case_id"], json.dumps(case, ensure_ascii=False), now_stamp()),
        )
    conn.close()

def load_cases() -> list[dict]:
    conn = db_connect()
    rows = conn.execute("SELECT data_json FROM invoices ORDER BY case_id").fetchall()
    conn.close()
    if rows:
        return [json.loads(row[0]) for row in rows]
    seeded = [dict(c, history=list(c["history"])) for c in SEED_CASES]
    conn = db_connect()
    with conn:
        for case in seeded:
            conn.execute("INSERT OR REPLACE INTO invoices(case_id,data_json,updated_at) VALUES(?,?,?)",
                         (case["case_id"], json.dumps(case, ensure_ascii=False), now_stamp()))
    conn.close()
    return seeded

def save_payment_plan(plan: dict | None) -> None:
    conn = db_connect()
    with conn:
        if plan is None:
            conn.execute("DELETE FROM app_state WHERE state_key = 'payment_plan'")
        else:
            conn.execute(
                "INSERT INTO app_state(state_key,state_json,updated_at) VALUES('payment_plan',?,?) "
                "ON CONFLICT(state_key) DO UPDATE SET state_json=excluded.state_json, updated_at=excluded.updated_at",
                (json.dumps(plan, ensure_ascii=False), now_stamp()),
            )
    conn.close()

def load_payment_plan() -> dict | None:
    conn = db_connect()
    row = conn.execute("SELECT state_json FROM app_state WHERE state_key='payment_plan'").fetchone()
    conn.close()
    return json.loads(row[0]) if row else None

def all_audit_events() -> pd.DataFrame:
    conn = db_connect()
    rows = conn.execute("SELECT timestamp AS Timestamp, case_id AS 'Case ID', vendor AS Vendor, event AS Event FROM audit_events ORDER BY id DESC").fetchall()
    conn.close()
    return pd.DataFrame(rows, columns=["Timestamp", "Case ID", "Vendor", "Event"])

def duplicate_match(candidate: dict, existing_cases: list[dict]) -> dict | None:
    # Exact duplicate: normalized vendor + invoice number.
    inv = norm(candidate.get("invoice_no"))
    vendor = norm(candidate.get("vendor"))
    for item in existing_cases:
        if inv and norm(item.get("invoice_no")) == inv and norm(item.get("vendor")) == vendor:
            return item
    return None

def possible_duplicate_match(candidate: dict, existing_cases: list[dict]) -> dict | None:
    # Conservative safeguard: same invoice number under a different vendor is suspicious.
    # This is not proof of duplication; it is held for human review.
    inv = norm(candidate.get("invoice_no"))
    vendor = norm(candidate.get("vendor"))
    if not inv:
        return None
    for item in existing_cases:
        if norm(item.get("invoice_no")) == inv and norm(item.get("vendor")) != vendor:
            return item
    return None

def match_po_grn(case: dict) -> tuple[bool, str]:
    if not str(case.get("po") or "").strip():
        return False, "Purchase order reference is missing."
    if not str(case.get("grn") or "").strip():
        return False, "Goods receipt note reference is missing."
    invoice_amount = finite_number(case.get("amount"))
    if invoice_amount is None or invoice_amount <= 0:
        return False, "Invoice amount must be a finite number greater than zero."
    for label, raw_amount in (("PO", case.get("po_amount")), ("GRN", case.get("grn_amount"))):
        ref_amount = finite_number(raw_amount)
        if ref_amount is None or ref_amount <= 0:
            return False, f"{label} amount is missing or invalid."
        ratio = abs(invoice_amount - ref_amount) / ref_amount
        if not math.isfinite(ratio) or ratio > PO_GRN_TOLERANCE:
            return False, f"Invoice differs from {label} amount by {ratio:.1%}, beyond ±{PO_GRN_TOLERANCE:.0%}."
    return True, "Invoice amount matches entered PO and GRN amounts within ±2%."

def invoice_approval_required(case: dict) -> bool:
    return case.get("status") in {"Ready for approval", "Pending CFO approval"}

def invoice_approval_check(case: dict, approver_role: str) -> tuple[bool, str]:
    """Revalidate the invoice at approval time; role selection is only a demo control."""
    if not invoice_approval_required(case):
        return False, "Invoice is not in an approvable status."
    if case.get("duplicate"):
        return False, "Duplicate flag blocks approval."
    if case.get("bank_changed"):
        return False, "Bank-detail change requires independent verification."
    amount = finite_number(case.get("amount", 0))
    confidence = finite_number(case.get("scan_conf", 0))
    if amount is None or confidence is None:
        return False, "Amount or extraction confidence is invalid."
    if amount <= 0:
        return False, "Invoice amount must be greater than zero."
    if confidence < CONFIDENCE_THRESHOLD or confidence > 1:
        return False, "Extraction confidence is below the required threshold."
    if not valid_demo_gstin(case.get("gstin", "")):
        return False, "GSTIN format check failed."
    match_ok, match_message = match_po_grn(case)
    if not match_ok:
        return False, match_message
    high_value = amount > HIGH_VALUE_THRESHOLD
    if high_value and approver_role != "CFO approver":
        return False, "This high-value invoice requires the CFO approver role in this demo."
    if case.get("status") == "Pending CFO approval" and not high_value:
        return False, "Approval status is inconsistent with the configured threshold; review the case."
    exception = str(case.get("exception", "") or "").strip()
    is_cfo_routing_note = (case.get("status") == "Pending CFO approval" and high_value and ("CFO" in exception.upper() or "threshold" in exception.casefold()))
    if exception and not is_cfo_routing_note:
        return False, "An unresolved exception blocks approval."
    return True, "All approval checks passed."


def payment_eligible(case: dict) -> bool:
    # Defensive checks: malformed persisted data must fail closed, never crash or pass.
    if case.get("status") != "Approved for payment":
        return False
    if case.get("bank_changed", False) or case.get("duplicate", False):
        return False
    if str(case.get("exception", "") or "").strip():
        return False
    amount = finite_number(case.get("amount", 0))
    confidence = finite_number(case.get("scan_conf", 0))
    if amount is None or confidence is None:
        return False
    if amount <= 0 or confidence < CONFIDENCE_THRESHOLD or confidence > 1:
        return False
    return valid_demo_gstin(case.get("gstin", "")) and match_po_grn(case)[0]


def validate_payment_plan(plan: dict, cases: list[dict]) -> tuple[bool, str]:
    """Validate saved plan against current invoice state and its stated cash cap."""
    by_id = {str(c.get("case_id")): c for c in cases}
    ids = plan.get("cases", [])
    if not isinstance(ids, list) or not ids or any(not isinstance(cid, (str, int)) for cid in ids):
        return False, "Payment plan has no valid invoice list."
    ids = [str(cid) for cid in ids]
    if len(ids) != len(set(ids)):
        return False, "Payment plan contains duplicate case IDs."
    if any(cid not in by_id for cid in ids):
        return False, "One or more planned invoices no longer exist."
    selected = [by_id[cid] for cid in ids]
    if any(not payment_eligible(c) for c in selected):
        return False, "One or more invoices are no longer eligible. Rebuild the plan."
    cash = finite_number(plan.get("cash", -1))
    stated_total = finite_number(plan.get("total", -1))
    amounts = [finite_number(c.get("amount")) for c in selected]
    if cash is None or stated_total is None or any(amount is None for amount in amounts):
        return False, "Payment plan contains invalid amounts."
    actual_total = sum(amounts)
    if cash < 0 or stated_total < 0 or not math.isclose(actual_total, stated_total, rel_tol=0, abs_tol=0.005):
        return False, "Plan total does not match the current invoice amounts. Rebuild the plan."
    if actual_total > cash:
        return False, "Plan total exceeds its cash limit. Rebuild the plan."
    return True, "Payment plan validated. No payment will be executed."

def kpi_snapshot(items: list[dict]) -> dict:
    total = len(items)
    status_counts = [str(c.get("status", "")) for c in items]
    return {
        "total": total,
        "ready": sum(s == "Ready for approval" for s in status_counts),
        "approved": sum(s == "Approved for payment" for s in status_counts),
        "blocked": sum("Blocked" in s for s in status_counts),
        "exceptions": sum(("Exception" in s or "Pending" in s or "human review" in s) for s in status_counts),
        "payment_eligible": sum(payment_eligible(c) for c in items),
        "queue_ready_rate": (sum(s == "Ready for approval" for s in status_counts) / total * 100) if total else 0.0,
    }

def init_state():
    if "cases" not in st.session_state:
        st.session_state.cases = load_cases()
    if "payment_plan" not in st.session_state:
        st.session_state.payment_plan = load_payment_plan()
    if "next_case_number" not in st.session_state:
        ids = [int(c["case_id"].split("-")[-1]) for c in st.session_state.cases if c.get("case_id", "").startswith("INV-") and c["case_id"].split("-")[-1].isdigit()]
        st.session_state.next_case_number = max(ids, default=1006) + 1

init_state()
cases = st.session_state.cases

st.markdown("""
<style>
.block-container {padding-top:1.4rem;max-width:1500px;}
.hero {padding:25px 30px;border-radius:17px;background:linear-gradient(115deg,#111d3a,#1c4770);color:white;margin-bottom:16px;}
.hero h1 {font-size:2.05rem;margin:0 0 8px 0;color:white;}
.hero p {font-size:1rem;color:#dbeafe;margin:0;}
div[data-testid="stMetric"] {border:1px solid #e5eaf2;padding:14px;border-radius:12px;}
</style>
""", unsafe_allow_html=True)
st.markdown('<div class="hero"><h1>Meridian | AP Automation Control Center</h1><p>Invoice intake • Transparent validation • Human approval • Cash-aware payment planning</p></div>', unsafe_allow_html=True)
st.caption("Coursework prototype • Synthetic data only • No live ERP, email, GST portal, or bank connection")

with st.sidebar:
    st.title("Navigation")
    page = st.radio("Open workspace", ["Overview", "Invoice queue", "Process an invoice", "Approvals", "Payment planner", "Agent workflow lab", "Exception workbench", "Reconciliation lab", "Test lab", "Audit & KPI report", "About the system"])
    st.divider()
    st.caption("Demo guardrails")
    st.write("🔒 Invoice approval is separate from plan approval")
    st.write("🛑 Duplicates and bank changes are held")
    st.write("🗃️ SQLite stores cases and audit events locally")
    st.write("🧪 Synthetic data only")
    if st.button("Reset to sample data", help="Deletes local demo records and restores six synthetic sample invoices."):
        conn = db_connect()
        with conn:
            conn.execute("DELETE FROM invoices")
            conn.execute("DELETE FROM audit_events")
            conn.execute("DELETE FROM app_state")
        conn.close()
        for key in ("cases", "payment_plan", "next_case_number"):
            st.session_state.pop(key, None)
        init_state()
        st.rerun()

k = kpi_snapshot(cases)
cols = st.columns(5)
cols[0].metric("Invoices in queue", k["total"])
cols[1].metric("Ready for approval", k["ready"])
cols[2].metric("Approved for payment", k["approved"])
cols[3].metric("Blocked", k["blocked"])
cols[4].metric("Exceptions / pending", k["exceptions"])
st.caption("Queue metrics are live status counts. They are not a production straight-through-processing result.")

if page == "Overview":
    st.subheader("Operations overview")
    left, right = st.columns([1.2, 1])
    with left:
        st.markdown("#### Four workflow modules")
        for num, title, desc in [
            ("01", "Intake", "Create a case and capture synthetic invoice fields."),
            ("02", "Validation", "Check duplicates, bank changes, extraction confidence, GSTIN format and PO/GRN amounts."),
            ("03", "Approval", "Require a separate human decision; route high-value invoices to CFO review."),
            ("04", "Payment planning & audit", "Plan only separately approved invoices; persist cases and audit events in SQLite."),
        ]:
            st.markdown(f'<div style="border:1px solid #dbe3ef;border-radius:12px;padding:13px 16px;margin-bottom:9px"><b>{num} · {title}</b><div style="font-size:.9rem">{desc}</div></div>', unsafe_allow_html=True)
    with right:
        st.markdown("#### Current status distribution")
        st.bar_chart(pd.Series([c["status"] for c in cases]).value_counts())
        st.markdown("#### Illustrative project targets")
        st.write("• STP target: ≥60%")
        st.write("• Early-payment discount capture: ≥80%")
        st.write("• Seeded exception detection: 100%")
        st.caption("These are proposal targets, not measured outcomes.")
    st.info("This app implements transparent rule-based modules. It does not currently run a LangGraph graph or an LLM.")

elif page == "Invoice queue":
    st.subheader("Invoice queue")
    query = st.text_input("Search vendor, invoice number or case ID").strip().casefold()
    statuses = ["All"] + sorted({c["status"] for c in cases})
    status_filter = st.selectbox("Filter by status", statuses)
    filtered = [c for c in cases if query in f'{c["vendor"]} {c["invoice_no"]} {c["case_id"]}'.casefold() and (status_filter == "All" or c["status"] == status_filter)]
    rows = [{"Case ID":c["case_id"],"Vendor":c["vendor"],"Invoice":c["invoice_no"],"Amount":money(c["amount"]),"Due date":c["due_date"],"Stage":c["stage"],"Status":c["status"]} for c in filtered]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    if filtered:
        chosen_id = st.selectbox("Inspect case", [c["case_id"] for c in filtered])
        case = next(c for c in filtered if c["case_id"] == chosen_id)
        a, b = st.columns(2)
        with a:
            st.markdown("#### Invoice details")
            for label, value in [
                ("Vendor", case["vendor"]), ("Invoice number", case["invoice_no"]),
                ("Invoice date", case["invoice_date"]), ("Amount", money(case["amount"])),
                ("PO / PO amount", f'{case.get("po") or "Missing"} / {money(case.get("po_amount"))}'),
                ("GRN / GRN amount", f'{case.get("grn") or "Missing"} / {money(case.get("grn_amount"))}'),
                ("GSTIN format", "Pass" if valid_demo_gstin(case.get("gstin","")) else "Hold"),
                ("GST / TDS demo rates", f'{case.get("gst_rate",0)}% / {case.get("tds_rate",0)}%'),
                ("Extraction confidence", f'{case.get("scan_conf",0):.0%}'),
            ]:
                st.write(f"**{label}:** {value}")
            ok, message = match_po_grn(case)
            st.write("**PO/GRN amount match:**", "Pass" if ok else "Hold", "—", message)
        with b:
            st.markdown("#### Decision and timeline")
            st.info(case["status"])
            st.write(case.get("exception") or "No active exception.")
            for event in case.get("history", []):
                st.write("• " + event)
    else:
        st.warning("No invoices match those filters.")
    st.download_button("Download visible queue CSV", pd.DataFrame(rows).to_csv(index=False).encode("utf-8-sig"), "invoice_queue.csv", "text/csv")

elif page == "Process an invoice":
    st.subheader("Process a synthetic invoice")
    with st.form("new_invoice_form"):
        a, b, c = st.columns(3)
        with a:
            vendor = st.text_input("Vendor name", "New Demo Vendor Pvt Ltd")
            invoice_no = st.text_input("Invoice number", f'DEMO-{st.session_state.next_case_number}')
            amount = st.number_input("Invoice amount (₹)", min_value=1, value=75000, step=1000)
            invoice_date = st.date_input("Invoice date", value=date.today())
        with b:
            po = st.text_input("PO reference", "PO-5001")
            po_amount = st.number_input("PO amount (₹)", min_value=0, value=75000, step=1000)
            grn = st.text_input("GRN reference", "GRN-5001")
            grn_amount = st.number_input("GRN amount (₹)", min_value=0, value=75000, step=1000)
            due_date = st.date_input("Due date", value=date.today())
        with c:
            gstin = st.text_input("GSTIN (format check only)", "29ABCDE1234F1Z5")
            confidence = st.slider("Extraction confidence", 0.0, 1.0, 0.97, 0.01)
            bank_changed = st.checkbox("Vendor bank details recently changed")
            known_duplicate = st.checkbox("Known duplicate invoice")
            gst_rate = st.selectbox("GST rate (demo)", [0,5,12,18,28], index=3)
            tds_rate = st.selectbox("TDS rate (demo)", [0,1,2,5,10], index=2)
        discount = st.number_input("Early-payment discount (%)", min_value=0.0, max_value=20.0, value=2.0, step=0.5)
        submitted = st.form_submit_button("Run validation workflow", type="primary")
    if submitted:
        if not vendor.strip() or not invoice_no.strip():
            st.error("Vendor and invoice number are required.")
        elif due_date < invoice_date:
            st.error("Due date cannot be earlier than invoice date.")
        else:
            cid = f'INV-{st.session_state.next_case_number}'
            st.session_state.next_case_number += 1
            candidate = {"vendor":vendor.strip(), "invoice_no":invoice_no.strip()}
            matched = duplicate_match(candidate, cases)
            possible_match = possible_duplicate_match(candidate, cases) if not matched else None
            duplicate = bool(known_duplicate or matched or possible_match)
            case = {
                "case_id":cid,"vendor":vendor.strip(),"invoice_no":invoice_no.strip(),
                "invoice_date":str(invoice_date),"amount":int(amount),"po":po.strip(),
                "po_amount":int(po_amount) if po_amount > 0 else None,"grn":grn.strip(),
                "grn_amount":int(grn_amount) if grn_amount > 0 else None,"gstin":gstin.strip().upper(),
                "gst_rate":int(gst_rate),"tds_rate":int(tds_rate),"due_date":str(due_date),
                "discount_pct":float(discount),"bank_changed":bool(bank_changed),"duplicate":duplicate,
                "scan_conf":float(confidence),"status":"Processing","stage":"Intake","exception":"",
                "query_attempts":0,"history":[],
            }
            log_event(case, "Intake created a synthetic invoice case")
            if duplicate:
                if matched:
                    detail = f"Exact duplicate vendor + invoice number; matched {matched['case_id']}."
                    status = "Blocked — duplicate"
                elif possible_match:
                    detail = f"Same invoice number appears under another vendor ({possible_match['case_id']}); human review required."
                    status = "Blocked — possible duplicate"
                else:
                    detail = "Manual duplicate flag selected; human review required."
                    status = "Blocked — duplicate"
                case.update(status=status,stage="Validation",exception=detail)
                log_event(case, "Duplicate or possible-duplicate guard triggered; held")
            elif bank_changed:
                case.update(status="Blocked — human review",stage="Validation",exception="Bank-detail changes require independent call-back confirmation.")
                log_event(case, "Bank-change guard triggered; held")
            elif confidence < CONFIDENCE_THRESHOLD:
                case.update(status="Exception — low-confidence extraction",stage="Data extraction",exception="Extraction confidence below 90%; review required.")
                log_event(case, "Low-confidence guard triggered; held")
            elif not valid_demo_gstin(gstin):
                case.update(status="Exception — GSTIN format",stage="Validation",exception="GSTIN format did not match the demo pattern; no live GST lookup is performed.")
                log_event(case, "GSTIN format check failed; held")
            else:
                match_ok, match_msg = match_po_grn(case)
                if not match_ok:
                    case.update(status="Exception — PO/GRN mismatch",stage="3-way match",exception=match_msg)
                    log_event(case, "PO/GRN check failed; held")
                elif amount > HIGH_VALUE_THRESHOLD:
                    case.update(status="Pending CFO approval",stage="Approval routing",exception=f"Amount exceeds {money(HIGH_VALUE_THRESHOLD)} demo threshold; CFO approval required.")
                    log_event(case, "Routed for CFO approval")
                else:
                    case.update(status="Ready for approval",stage="Approval routing",exception="")
                    log_event(case, "Validation passed; awaiting separate invoice approval")
            cases.append(case)
            st.session_state.cases = cases
            save_case(case)
            st.success(f'{cid}: {case["status"]}')
            st.info(case["exception"] or "Validation passed. The invoice is not yet approved for payment.")

elif page == "Approvals":
    st.subheader("Human invoice approval queue")
    approver_role = st.selectbox("Demo reviewer role", ["AP approver", "CFO approver"])
    st.caption("Role selection simulates authorization for coursework; it is not real identity or access management.")
    pending = [c for c in cases if invoice_approval_required(c)]
    if not pending:
        st.info("No invoices are awaiting invoice approval.")
    for case in pending:
        with st.expander(f'{case["case_id"]} · {case["vendor"]} · {money(case["amount"])} · {case["status"]}'):
            st.write(f'Invoice: {case["invoice_no"]} • Due: {case["due_date"]}')
            st.write(case.get("exception") or "Validation passed.")
            if case["status"] == "Pending CFO approval":
                st.warning("This case requires CFO-level authorization in the scenario.")
            if st.button("Approve invoice for payment", key=f'approve_{case["case_id"]}'):
                # Revalidate every safety guard immediately before changing approval status.
                approval_ok, approval_message = invoice_approval_check(case, approver_role)
                if not approval_ok:
                    st.error(f"Approval blocked: {approval_message}")
                else:
                    case.update(status="Approved for payment",stage="Invoice approved",exception="")
                    log_event(case, f'{approver_role} approved invoice for payment planning; no payment executed')
                    save_case(case)
                    st.success("Invoice approved in the demo. No payment was executed.")
                    st.rerun()
            if st.button("Reject invoice", key=f'reject_{case["case_id"]}'):
                case.update(status="Rejected",stage="Approval rejected",exception="Rejected by demo human reviewer.")
                log_event(case, "Human demo reviewer rejected invoice")
                save_case(case)
                st.rerun()

elif page == "Payment planner":
    st.subheader("Cash-aware payment planner")
    st.write("Only invoices separately marked Approved for payment are eligible. Plan approval is separate; no payment is executed.")
    cash = st.number_input("Available cash (₹)", min_value=0, value=300000, step=25000)
    eligible = [c for c in cases if payment_eligible(c)]
    optimization = optimize_payment_run(eligible, cash)
    selected_ids = optimization["cases"]
    selected = [c for c in eligible if c["case_id"] in selected_ids]
    total = optimization["total"]
    m = st.columns(4)
    m[0].metric("Planned total", money(total))
    m[1].metric("Cash remaining", money(cash-total))
    m[2].metric("Invoices selected", len(selected))
    m[3].metric("Illustrative benefit", money(optimization["benefit"]))
    st.caption("Optimizer uses exact subset search on up to 20 eligible invoices. Benefit = early discount value + an illustrative 0.5% due/overdue penalty proxy; this is not a contractual penalty calculation.")
    if selected:
        st.dataframe(pd.DataFrame([{"Case ID":c["case_id"],"Vendor":c["vendor"],"Due date":c["due_date"],"Amount":money(c["amount"]),"Discount %":c["discount_pct"]} for c in selected]), use_container_width=True, hide_index=True)
        st.info(f"Potential discount value: {money(optimization['discount_value'])}; illustrative and conditional on contract terms. Penalty-avoidance proxy: {money(optimization['penalty_avoidance_proxy'])}.")
        if st.button("Submit plan for human approval", type="primary"):
            st.session_state.payment_plan = {"created":now_stamp(),"cash":float(cash),"total":float(total),"cases":[c["case_id"] for c in selected],"approved":False,"optimizer":optimization["optimizer"],"objective":optimization["objective"]}
            save_payment_plan(st.session_state.payment_plan)
            persist_audit({"timestamp":now_stamp(),"case_id":"PAYMENT-PLAN","vendor":"—","event":"Payment plan submitted for human approval"})
            st.success("Plan submitted. No payment has been released.")
    else:
        st.warning("No approved invoices fit the cash limit. Approve eligible invoices first.")
    plan = st.session_state.payment_plan
    if plan:
        st.divider()
        st.markdown("#### Latest payment plan")
        st.write(f'Created: {plan["created"]} • Total: {money(plan["total"])} • Status: {"Approved (demo only)" if plan["approved"] else "Awaiting human approval"}')
        st.write("Cases: " + (", ".join(plan["cases"]) if plan["cases"] else "None"))
        if not plan["approved"] and st.button("Approve demo payment plan"):
            plan_ok, plan_message = validate_payment_plan(plan, cases)
            if not plan_ok:
                st.error(f"Plan approval blocked: {plan_message}")
            else:
                plan["approved"] = True
                plan["approved_at"] = now_stamp()
                save_payment_plan(plan)
                persist_audit({"timestamp":now_stamp(),"case_id":"PAYMENT-PLAN","vendor":"—","event":"Demo payment plan approved; no payment executed"})
                for case in cases:
                    if case["case_id"] in plan["cases"]:
                        log_event(case, "Included in demo payment plan; no payment executed")
                        save_case(case)
                st.success("Demo plan approved. No real payment was executed.")

elif page == "Agent workflow lab":
    st.subheader("Four-agent end-to-end workflow lab")
    st.write("Runs the proposal's 20 synthetic scenarios through Intake, Validation/Exception, Approval and Payment/Reconciliation workflow modules. No real external systems are called.")
    engine_choice = st.radio("Orchestration engine", ["Deterministic offline engine", "LangGraph StateGraph (if installed)"], horizontal=True)
    use_langgraph = engine_choice.startswith("LangGraph")
    langgraph_runner = None
    if use_langgraph:
        try:
            from langgraph_runtime import run_langgraph as langgraph_runner
            st.success("LangGraph runtime is available. This uses a real StateGraph with deterministic nodes; no LLM or external service is connected.")
        except Exception as exc:
            st.error(f"LangGraph runtime is unavailable: {exc}. Install requirements.txt, or choose the deterministic offline engine.")
    else:
        st.info("Offline mode uses the same deterministic business rules without requiring LangGraph. All messages, replies, posting records and payment references are simulated.")
    if st.button("Run the 20-case proposal test set", type="primary", disabled=(use_langgraph and langgraph_runner is None)):
        suite = proposal_test_set()
        run_results = []
        trace_rows = []
        for c in suite:
            reply = None
            if c.get("exception_kind") == "price mismatch":
                reply = {"resolved":True,"to":"vendor","reply":"Synthetic vendor reply corrected the PO amount; the agent re-checks the match before continuing.","po_amount":c["amount"]}
            elif c.get("exception_kind") == "missing GRN":
                reply = {"resolved":True,"to":"warehouse/requester","reply":"Synthetic warehouse reply supplied GRN confirmation; the agent re-checks the match before continuing.","grn":f"GRN-CONF-{c['case_id']}","grn_amount":c["amount"]}
            if use_langgraph and langgraph_runner is not None:
                result = langgraph_runner(c, existing_cases=[], simulated_reply=reply, cash_limit=200000)
            else:
                result = run_graph(c, existing_cases=[], simulated_reply=reply, cash_limit=200000)
            is_stp = bool(result.get("auto_approved")) and result.get("status") in {"Approved for payment", "Payment planned — awaiting release approval"}
            run_results.append({"Case ID":c["case_id"],"Scenario":c["scenario"],"Expected behavior":c["expected_outcome"],"Actual status":result.get("status"),"STP before release approval":"Yes" if is_stp else "No","Query attempts":result.get("query_attempts",0),"Exception":result.get("exception","")})
            for item in result.get("trace",[]):
                trace_rows.append({"Case ID":c["case_id"],**item})
        st.session_state.workflow_results = run_results
        st.session_state.workflow_trace = trace_rows
    if st.session_state.get("workflow_results"):
        results_df = pd.DataFrame(st.session_state.workflow_results)
        st.dataframe(results_df, use_container_width=True, hide_index=True)
        total = len(results_df)
        stp = int((results_df["STP before release approval"] == "Yes").sum())
        c1,c2,c3 = st.columns(3)
        c1.metric("Cases executed", total)
        c2.metric("STP cases", f"{stp}/{total}")
        c3.metric("Measured STP rate", f"{(stp/total*100 if total else 0):.1f}%")
        st.caption("This is a measured result of the deterministic synthetic test run, not a real-world KPI. The proposal target is ≥60%. Price-mismatch and missing-GRN cases receive simulated replies, are revalidated, and may continue only if the match passes.")
        st.info("Interpretation: QUERY → RESOLVE → STP means an exception was detected, a synthetic reply corrected the missing/mismatched information, and validation passed on re-check. It is not an unresolved exception.")
        st.download_button("Download 20-case results CSV", results_df.to_csv(index=False).encode("utf-8-sig"), "meridian_20_case_results.csv", "text/csv")
        trace_df = pd.DataFrame(st.session_state.get("workflow_trace", []))
        with st.expander("View agent decision trace"):
            st.dataframe(trace_df, use_container_width=True, hide_index=True)
            st.download_button("Download agent trace CSV", trace_df.to_csv(index=False).encode("utf-8-sig"), "meridian_agent_trace.csv", "text/csv")

elif page == "Exception workbench":
    st.subheader("Exception workbench")
    unresolved = [c for c in cases if c.get("exception") and c.get("status","").startswith(("Exception", "Blocked", "Escalated", "Approval blocked"))]
    if not unresolved:
        st.info("No currently flagged exception cases. Create a synthetic case with a mismatch, missing GRN, duplicate or bank change in Process an invoice.")
    else:
        selected_id = st.selectbox("Select exception case", [c["case_id"] for c in unresolved])
        case = next(c for c in unresolved if c["case_id"] == selected_id)
        st.write(f"**{case['vendor']} · {case['invoice_no']} · {money(case['amount'])}**")
        st.error(case.get("exception", "Exception reason not recorded"))
        st.write(f"Query attempts recorded: {int(case.get('query_attempts',0))}/2")
        with st.form(f"resolve_{selected_id}"):
            st.markdown("**Record a synthetic requester/vendor response**")
            new_po = st.text_input("Corrected PO reference", str(case.get("po") or ""))
            new_po_amount = st.number_input("Confirmed PO amount (₹)", min_value=0.0, value=float(case.get("po_amount") or 0), step=1000.0)
            new_grn = st.text_input("Confirmed GRN reference", str(case.get("grn") or ""))
            new_grn_amount = st.number_input("Confirmed GRN amount (₹)", min_value=0.0, value=float(case.get("grn_amount") or 0), step=1000.0)
            reply_note = st.text_input("Response note", "Synthetic confirmation received")
            resolve_submit = st.form_submit_button("Apply response and revalidate")
        if resolve_submit:
            case["po"] = new_po.strip()
            case["po_amount"] = new_po_amount if new_po_amount > 0 else None
            case["grn"] = new_grn.strip()
            case["grn_amount"] = new_grn_amount if new_grn_amount > 0 else None
            case["query_attempts"] = min(2, int(case.get("query_attempts",0))+1)
            case.setdefault("history", []).append(f"{now_stamp()} — Synthetic query response: {reply_note}")
            match_ok, match_msg = match_po_grn(case)
            if match_ok and not case.get("bank_changed") and not case.get("duplicate") and valid_demo_gstin(case.get("gstin","")) and float(case.get("scan_conf",0)) >= CONFIDENCE_THRESHOLD:
                case["status"] = "Pending CFO approval" if float(case.get("amount",0)) > HIGH_VALUE_THRESHOLD else "Ready for approval"
                case["stage"] = "Approval routing"
                case["exception"] = f"Resolved after query attempt {case['query_attempts']}: {reply_note}"
                # Resolved context is recorded in history; clear active exception to allow revalidation at approval.
                case["history"].append(f"{now_stamp()} — Revalidation passed: {match_msg}")
                case["exception"] = ""
                log_event(case, "Exception resolved and revalidated; returned to approval queue")
                st.success("Exception resolved. The case is back in the approval queue.")
            else:
                case["exception"] = match_msg if not match_ok else "Bank change, duplicate, GSTIN or confidence control still requires review."
                if case["query_attempts"] >= 2:
                    case["status"] = "Escalated — human review"
                    case["stage"] = "Exception escalation"
                else:
                    case["status"] = "Exception — query attempted"
                    case["stage"] = "Exception resolution"
                log_event(case, f"Query attempt {case['query_attempts']} recorded; {case['status']}")
                st.warning(f"Still unresolved: {case['exception']}")
            save_case(case)
            st.rerun()
        if st.button("Record failed query attempt", key=f"failed_query_{selected_id}"):
            case["query_attempts"] = min(2, int(case.get("query_attempts",0))+1)
            case.setdefault("history", []).append(f"{now_stamp()} — Synthetic query attempt {case['query_attempts']} failed")
            if case["query_attempts"] >= 2:
                case["status"] = "Escalated — human review"
                case["stage"] = "Exception escalation"
                case["exception"] = f"Escalated after two failed query attempts: {case.get('exception','Exception unresolved')}"
            log_event(case, f"Failed query attempt {case['query_attempts']} recorded")
            save_case(case)
            st.rerun()

elif page == "Reconciliation lab":
    st.subheader("Synthetic bank reconciliation lab")
    st.write("Match simulated payment references to a synthetic bank statement. This is not a live bank connection and no payment is executed.")
    uploaded = st.file_uploader("Optional synthetic bank statement CSV (columns: reference, amount)", type=["csv"])
    payment_cases = [c for c in cases if isinstance(c.get("payment_stub"), dict)]
    if not payment_cases:
        st.info("No payment stubs found. Run the Agent workflow lab or build a plan from approved invoices first.")
    else:
        selected_payment_case = st.selectbox("Select simulated payment", [c["case_id"] for c in payment_cases])
        chosen_case = next(c for c in payment_cases if c["case_id"] == selected_payment_case)
        payment = chosen_case["payment_stub"]
        st.write(f"Payment reference: `{payment.get('reference')}` · Amount: **{money(payment.get('amount'))}**")
        if uploaded is not None:
            try:
                bank_df = pd.read_csv(uploaded)
                if not {"reference", "amount"}.issubset({str(col).strip().casefold() for col in bank_df.columns}):
                    st.error("CSV must contain reference and amount columns.")
                    bank_rows = []
                else:
                    bank_df.columns = [str(col).strip().casefold() for col in bank_df.columns]
                    bank_rows = bank_df.to_dict(orient="records")
            except Exception as exc:
                st.error(f"Could not read CSV: {exc}")
                bank_rows = []
        else:
            bank_rows = [{"reference":payment.get("reference"),"amount":payment.get("amount")}] if st.checkbox("Use a generated matching sample row", value=True) else []
        if st.button("Reconcile selected payment"):
            result = reconcile_payment(payment, bank_rows)
            chosen_case["reconciliation_status"] = result["status"]
            chosen_case.setdefault("history", []).append(f"{now_stamp()} — Reconciliation: {result['status']} — {result['reason']}")
            log_event(chosen_case, f"Reconciliation result: {result['status']} — {result['reason']}")
            save_case(chosen_case)
            st.session_state.last_reconciliation = {"case_id":selected_payment_case, **result}
            st.rerun()
        if st.session_state.get("last_reconciliation", {}).get("case_id") == selected_payment_case:
            rec = st.session_state.last_reconciliation
            if rec["status"] == "Matched": st.success(rec["reason"])
            elif rec["status"] == "Unmatched": st.warning(rec["reason"])
            else: st.error(rec["reason"])

elif page == "Test lab":
    st.subheader("Validation test lab")
    st.write("Runs 20 deterministic checks against synthetic scenarios. This is a rule test, not an independent production benchmark.")

    def _test_case(name, case, expected, detail=""):
        return {"name": name, "case": case, "expected": expected, "detail": detail}

    def _base_test_case(vendor, invoice_no, amount=1000, po="PO-1", po_amount=1000,
                        grn="GRN-1", grn_amount=1000, gstin="29ABCDE1234F1Z5",
                        scan_conf=0.98, bank_changed=False, duplicate=False):
        return {"vendor": vendor, "invoice_no": invoice_no, "amount": amount,
                "po": po, "po_amount": po_amount, "grn": grn,
                "grn_amount": grn_amount, "gstin": gstin,
                "scan_conf": scan_conf, "bank_changed": bank_changed,
                "duplicate": duplicate}

    test_cases = [
        _test_case("Valid synthetic invoice", _base_test_case("A", "A-1"), "PASS"),
        _test_case("Exact duplicate flagged", _base_test_case("A", "A-2", duplicate=True), "BLOCK"),
        _test_case("Vendor bank details changed", _base_test_case("B", "B-1", bank_changed=True), "BLOCK"),
        _test_case("Low extraction confidence", _base_test_case("C", "C-1", scan_conf=0.60), "BLOCK"),
        _test_case("Invalid GSTIN format", _base_test_case("D", "D-1", gstin="NOT-A-GSTIN"), "BLOCK"),
        _test_case("PO amount mismatch above 2%", _base_test_case("E", "E-1", amount=1200, po_amount=1000, grn_amount=1200), "BLOCK"),
        _test_case("Missing GRN reference", _base_test_case("F", "F-1", grn="", grn_amount=None), "BLOCK"),
        _test_case("Missing PO reference", _base_test_case("G", "G-1", po=""), "BLOCK"),
        _test_case("Zero invoice amount", _base_test_case("H", "H-1", amount=0, po_amount=1000, grn_amount=1000), "BLOCK"),
        _test_case("Negative invoice amount", _base_test_case("I", "I-1", amount=-100), "BLOCK"),
        _test_case("Missing PO amount", _base_test_case("J", "J-1", po_amount=None), "BLOCK"),
        _test_case("Missing GRN amount", _base_test_case("K", "K-1", grn_amount=None), "BLOCK"),
        _test_case("Confidence exactly at 90% threshold", _base_test_case("L", "L-1", scan_conf=0.90), "PASS"),
        _test_case("Confidence above valid range", _base_test_case("M", "M-1", scan_conf=1.01), "BLOCK"),
        _test_case("PO variance exactly 2%", _base_test_case("N", "N-1", amount=1020, po_amount=1000, grn_amount=1020), "PASS"),
        _test_case("GRN variance above 2%", _base_test_case("O", "O-1", amount=1000, po_amount=1000, grn_amount=1100), "BLOCK"),
        _test_case("Lowercase GSTIN normalized for format check", _base_test_case("P", "P-1", gstin="29abcde1234f1z5"), "PASS"),
        _test_case("Whitespace-only PO reference", _base_test_case("Q", "Q-1", po="   "), "BLOCK"),
        _test_case("Invalid PO amount zero", _base_test_case("R", "R-1", po_amount=0), "BLOCK"),
        _test_case("Valid invoice with bank change and duplicate flags", _base_test_case("S", "S-1", bank_changed=True, duplicate=True), "BLOCK"),
    ]

    def _validation_test_result(case):
        if case.get("duplicate"):
            return "BLOCK", "Duplicate flag is set; invoice must be held for human review."
        if case.get("bank_changed"):
            return "BLOCK", "Vendor bank details changed; independent verification is required."
        confidence = finite_number(case.get("scan_conf"))
        if confidence is None or confidence < CONFIDENCE_THRESHOLD or confidence > 1:
            return "BLOCK", "Extraction confidence is missing, below 90%, or outside the valid 0–1 range."
        if not valid_demo_gstin(case.get("gstin", "")):
            return "BLOCK", "GSTIN format check failed; this is not a live GST registration lookup."
        match_ok, match_message = match_po_grn(case)
        if not match_ok:
            return "BLOCK", match_message
        return "PASS", "All configured demo validation guards passed."

    results = []
    for test in test_cases:
        actual, reason = _validation_test_result(test["case"])
        results.append({"Test": test["name"], "Expected": test["expected"],
                        "Actual": actual, "Result": "PASS" if actual == test["expected"] else "FAIL",
                        "Explanation": reason})
    test_df = pd.DataFrame(results)
    st.dataframe(test_df, use_container_width=True, hide_index=True)
    total = len(test_df)
    passed = int((test_df["Result"] == "PASS").sum())
    failed = total - passed
    c1, c2, c3 = st.columns(3)
    c1.metric("Scenarios executed", total)
    c2.metric("Passed", passed)
    c3.metric("Failed", failed, delta=None if failed == 0 else f"{failed} need review", delta_color="inverse")
    st.metric("Test pass rate", f"{(passed / total * 100 if total else 0):.1f}%")
    st.download_button("Download validation test results CSV", test_df.to_csv(index=False).encode("utf-8-sig"), "meridian_validation_test_results.csv", "text/csv")
    st.caption("These are hand-authored synthetic rule scenarios. Passing means the configured expected rule outcome matched; it does not establish real-world accuracy, legal GST compliance, or production readiness.")

elif page == "Audit & KPI report":
    st.subheader("Audit trail & KPI report")
    audit_df = all_audit_events()
    st.dataframe(audit_df, use_container_width=True, hide_index=True)
    st.download_button("Export persistent audit log CSV", audit_df.to_csv(index=False).encode("utf-8-sig"), "audit_log.csv", "text/csv")
    st.markdown("#### Live queue metrics")
    snap = kpi_snapshot(cases)
    cols = st.columns(4)
    cols[0].metric("Cases", snap["total"])
    cols[1].metric("Awaiting invoice approval", snap["ready"])
    cols[2].metric("Approved for payment", snap["approved"])
    cols[3].metric("Eligible for planning", snap["payment_eligible"])
    st.markdown("#### Proposal targets (not measured results)")
    st.dataframe(pd.DataFrame([
        {"KPI":"Cycle time per invoice","Baseline assumption":"5 days","Target":"≤1 day","Measured by app?":"No"},
        {"KPI":"Straight-through processing","Baseline assumption":"0%","Target":"≥60%","Measured by app?":"No"},
        {"KPI":"Human touchpoints per invoice","Baseline assumption":"4","Target":"≤1.5","Measured by app?":"No"},
        {"KPI":"Error / rework rate","Baseline assumption":"4%","Target":"<1%","Measured by app?":"No"},
        {"KPI":"Seeded exceptions caught","Baseline assumption":"About 50%","Target":"100%","Measured by app?":"Test lab only; not production"},
        {"KPI":"Early-payment discounts captured","Baseline assumption":"30%","Target":"≥80%","Measured by app?":"No"},
        {"KPI":"Cost per invoice","Baseline assumption":"₹250","Target":"≤₹80","Measured by app?":"No"},
    ]), use_container_width=True, hide_index=True)
    st.caption("SQLite persists cases and audit events locally in meridian_demo.sqlite3. Keep that file if you need the demo state; reset-to-sample deletes its records.")

elif page == "About the system":
    st.subheader("System design and limitations")
    st.markdown("""
**Business context:** Meridian Components Pvt. Ltd. is an illustrative scenario: approximately 8,000 supplier invoices per month from 600 vendors. These figures are assumptions for coursework, not verified company data.

**Workflow modules**
1. **Intake:** create a case and capture synthetic invoice fields.
2. **Validation:** exact and suspicious duplicate holds, bank-change holds, confidence threshold, GSTIN-format check, and PO/GRN amount matching.
3. **Approval:** separate invoice-level human approval; high-value cases require the CFO reviewer role in this demo (not real access control).
4. **Payment planning and audit:** only separately approved invoices can be planned; approval of a demo plan never sends a payment.

**Technical implementation:** Python, Streamlit, SQLite persistence for invoice records, audit events and the latest payment plan, transparent rule-based checks and downloadable CSV reports.

**Limitations:** The Agent workflow lab can optionally run a real LangGraph StateGraph when LangGraph is installed; its nodes still use deterministic Python business rules and do not use an LLM. The offline engine is a conditional rule-based state machine. This prototype does not implement real OCR/LLM extraction, vendor-master verification, line-item or quantity matching, live GST portal verification, statutory GST/TDS calculation, real email queries, ERP posting, live bank-statement access, or real bank payments. GSTIN validation checks format only. The ±2% comparison uses amounts manually entered in this demo. KPI targets are not measured operational outcomes. Do not enter real or confidential data.
""")
    st.write(f"Version: `{APP_VERSION}`")
    st.write(f"Local database file: `{DB_PATH.name}`")
    st.warning("Synthetic coursework prototype only. Never use it to authorize real payments.")

st.divider()
st.caption("Meridian AP Automation • Coursework prototype • Local SQLite database • No real payments executed.")
