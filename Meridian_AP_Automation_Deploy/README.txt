# Meridian AP Automation — Rubric-Checked Coursework Prototype — Updated Test Lab

See `RUBRIC_ALIGNMENT_CHECK.txt` for a criterion-by-criterion checklist and the items that still need separate report/proposal/presentation evidence.

## What is included
- `app.py`: Streamlit interface, local SQLite persistence, invoice intake/queue, approval controls, payment planner, workflow lab, exception workbench, reconciliation lab, audit report, and an expanded 20-scenario validation Test Lab with reason-specific explanations and CSV export.
- `langgraph_runtime.py`: optional real LangGraph `StateGraph` orchestration using deterministic business-rule nodes; no external services are contacted.
- `workflow_engine.py`: four specialized deterministic agent modules (Intake, Validation/Exception, Approval, Payment & Reconciliation), conditional graph-style orchestration, query attempt cap, payment-run optimization, reconciliation matching, and 20 synthetic proposal scenarios.
- `test_meridian_rules.py`: tests for the existing app validation and payment-plan rules.
- `test_workflow_engine.py`: tests for workflow routing, duplicates, confidence, approval, exception retry cap, optimization, reconciliation, and 20-case STP measurement.
- `test_langgraph_runtime.py`: checks the LangGraph runtime entry point or the clear missing-dependency message, internal state schema fields, and conditional-routing behavior.
- `requirements.txt`: Python packages.

## Validation Test Lab update
- The Test Lab now executes 20 hand-authored synthetic validation scenarios, reports pass/fail totals and pass rate, explains the specific guard that blocked each case, and exports a CSV.
- Test outcomes verify configured demo rules only; they are not production accuracy or tax-compliance evidence.

## Install and run
Use Python 3.10 or newer. Open Command Prompt/Terminal in the extracted project folder:

```bash
python -m pip install -r requirements.txt
python -m py_compile app.py workflow_engine.py langgraph_runtime.py test_meridian_rules.py test_workflow_engine.py test_langgraph_runtime.py
python -m unittest test_meridian_rules.py test_workflow_engine.py test_langgraph_runtime.py -v
streamlit run app.py
```

Then open the local URL printed in the terminal (normally http://localhost:8501).

## Suggested demo order
1. Overview — explain the four-agent design and safeguards.
2. Agent workflow lab — run all 20 synthetic scenarios; inspect measured results and agent trace.
3. Invoice queue / Process an invoice — demonstrate a clean invoice, a duplicate, a low-confidence scan, a missing GRN, or a bank change.
4. Exception workbench — record synthetic query attempts and resolve or escalate an exception.
5. Approvals — demonstrate the AP/CFO threshold and separation of duties.
6. Payment planner — show cash-constrained subset optimization and submit a plan for human approval.
7. Reconciliation lab — match a simulated payment reference to a generated or uploaded synthetic statement row.
8. Audit & KPI report — export traceable events and distinguish measured test results from proposal targets.

## Important accuracy and safety notes
- Synthetic data only. Do not enter real supplier, tax, bank, personal, or confidential information.
- The workflow lab offers an optional real LangGraph StateGraph runtime when LangGraph is installed. The graph nodes are deterministic Python business-rule functions; there is no LLM runtime. This is actual graph orchestration, but it is not a live LLM-powered autonomous deployment.
- No real OCR, mailbox, vendor master, GST portal, ERP, bank, journal posting, or payment release is connected. All email replies, payment references and journal entries are simulated.
- GSTIN validation is format-only, not a live GST registration check or tax advice. GST/TDS rates are illustrative and do not calculate statutory liability.
- PO/GRN matching compares synthetic amounts entered in the app; it does not match line items or quantities against an ERP.
- The payment optimizer searches exact subsets among up to 20 eligible invoices. The objective combines entered early-discount value with an explicitly illustrative 0.5% proxy for due/overdue penalty avoidance. It is not a legal/contractual penalty model.
- The approval role selector is a demo control, not secure authentication. Never use this prototype to approve or execute real payments.
- The 20-case STP result is measured on a synthetic deterministic test set. It is not a production accuracy or ROI claim. Proposal baseline figures are assumptions until a timed manual baseline is measured.
- SQLite database `meridian_demo.sqlite3` is created beside `app.py` and retains cases, plan state and audit events. Keep it to preserve demo data. Reset-to-sample deletes demo records.
- Automated tests do not replace a manual browser walkthrough. Verify all UI pages and actions on your own machine before the viva. The LangGraph library is not installed in some offline environments; install requirements before testing the real StateGraph route. The optional graph state explicitly declares internal demo context keys to ensure they survive LangGraph state validation.

Workflow test-set note: price-mismatch and missing-GRN scenarios intentionally receive a simulated corrective reply, are revalidated, and can then continue to payment planning. Their expected outcome is labelled QUERY → RESOLVE → STP. If the simulated reply does not resolve the mismatch, the workflow should remain in exception handling or escalate.
