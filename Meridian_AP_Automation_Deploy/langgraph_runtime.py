"""Optional real LangGraph orchestration for the Meridian coursework prototype.

The existing deterministic workflow_engine remains the offline/testable domain layer.
This module wraps its specialist functions in an actual LangGraph StateGraph when
LangGraph is installed. No LLM, email, ERP, tax portal, or bank is contacted.
"""
from __future__ import annotations
from typing import Any, TypedDict

from workflow_engine import (
    intake_agent, validation_agent, exception_agent,
    approval_agent, payment_reconciliation_agent,
)

try:
    from langgraph.graph import StateGraph, END
except ImportError as exc:  # clear error rather than silently pretending to use LangGraph
    StateGraph = None  # type: ignore[assignment,misc]
    END = "__end__"
    _LANGGRAPH_IMPORT_ERROR = exc
else:
    _LANGGRAPH_IMPORT_ERROR = None


class InvoiceState(TypedDict, total=False):
    case_id: str
    vendor: str
    invoice_no: str
    amount: float
    po: str
    po_amount: float
    grn: str
    grn_amount: float
    gstin: str
    scan_conf: float
    due_date: str
    discount_pct: float
    bank_changed: bool
    duplicate: bool
    exception: str
    status: str
    next_node: str
    trace: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    query_attempts: int
    stage: str
    human_touchpoints: int
    required_approver: str
    auto_approved: bool
    payment_stub: dict[str, Any]
    posting_stub: dict[str, Any]
    reconciliation_status: str
    graph_completed_at: str
    # Internal demo context must be declared in the graph schema; otherwise
    # LangGraph may discard these keys before context-aware nodes can read them.
    _existing_cases: list[dict[str, Any]]
    _approver_role: str | None
    _approve: bool
    _cash_limit: float | None
    _simulated_reply: dict[str, Any] | None


def _require_langgraph() -> None:
    if StateGraph is None:
        raise RuntimeError(
            "LangGraph is not installed. Install project requirements with "
            "`python -m pip install -r requirements.txt` to run the real StateGraph."
        ) from _LANGGRAPH_IMPORT_ERROR


def build_langgraph():
    """Build and return the compiled LangGraph StateGraph app."""
    _require_langgraph()
    graph = StateGraph(InvoiceState)
    graph.add_node("intake", intake_agent)
    graph.add_node("validation", validation_agent)
    graph.add_node("exception_resolution", exception_agent)
    graph.add_node("approval", approval_agent)
    graph.add_node("payment_reconciliation", payment_reconciliation_agent)

    graph.set_entry_point("intake")
    graph.add_conditional_edges("intake", _after_intake, {"validation": "validation", "end": END})
    graph.add_conditional_edges("validation", _after_validation, {
        "exception_resolution": "exception_resolution", "approval": "approval", "end": END,
    })
    graph.add_conditional_edges("exception_resolution", _after_exception, {
        "exception_resolution": "exception_resolution", "approval": "approval", "end": END,
    })
    graph.add_conditional_edges("approval", _after_approval, {
        "payment_reconciliation": "payment_reconciliation", "end": END,
    })
    graph.add_edge("payment_reconciliation", END)
    return graph.compile()


def _after_intake(state: InvoiceState) -> str:
    return "end" if state.get("next_node") == "END" else "validation"


def _after_validation(state: InvoiceState) -> str:
    nxt = state.get("next_node")
    if nxt == "exception":
        return "exception_resolution"
    if nxt in {"approval", "human_approval"}:
        return "approval"
    return "end"


def _after_exception(state: InvoiceState) -> str:
    nxt = state.get("next_node")
    if nxt == "exception":
        return "exception_resolution"
    if nxt in {"approval", "human_approval"}:
        return "approval"
    return "end"


def _after_approval(state: InvoiceState) -> str:
    return "payment_reconciliation" if state.get("next_node") == "payment" else "end"


def run_langgraph(invoice: dict[str, Any], *, existing_cases: list[dict[str, Any]] | None = None,
                  approver_role: str | None = None, approve: bool = False,
                  cash_limit: float | None = None,
                  simulated_reply: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run a real LangGraph graph with deterministic nodes and injected demo context.

    Extra arguments are placed in state under private demo keys and removed from the
    returned result. This is synthetic orchestration only; approval controls are demo
    parameters, not authentication.
    """
    initial = dict(invoice)
    initial["_existing_cases"] = existing_cases or []
    initial["_approver_role"] = approver_role
    initial["_approve"] = bool(approve)
    initial["_cash_limit"] = cash_limit
    initial["_simulated_reply"] = simulated_reply
    # Wrap node calls so the domain functions receive the appropriate context.
    # The graph is rebuilt with context-aware nodes to avoid global mutable state.
    _require_langgraph()
    graph = StateGraph(InvoiceState)
    graph.add_node("intake", lambda s: intake_agent(s))
    graph.add_node("validation", lambda s: validation_agent(s, s.get("_existing_cases", [])))
    graph.add_node("exception_resolution", lambda s: exception_agent(s, s.get("_simulated_reply")))
    graph.add_node("approval", lambda s: approval_agent(s, s.get("_approver_role"), s.get("_approve", False)))
    graph.add_node("payment_reconciliation", lambda s: payment_reconciliation_agent(s, s.get("_cash_limit")))
    graph.set_entry_point("intake")
    graph.add_conditional_edges("intake", _after_intake, {"validation": "validation", "end": END})
    graph.add_conditional_edges("validation", _after_validation, {"exception_resolution": "exception_resolution", "approval": "approval", "end": END})
    graph.add_conditional_edges("exception_resolution", _after_exception, {"exception_resolution": "exception_resolution", "approval": "approval", "end": END})
    graph.add_conditional_edges("approval", _after_approval, {"payment_reconciliation": "payment_reconciliation", "end": END})
    graph.add_edge("payment_reconciliation", END)
    result = graph.compile().invoke(initial)
    result.pop("_existing_cases", None)
    result.pop("_approver_role", None)
    result.pop("_approve", None)
    result.pop("_cash_limit", None)
    result.pop("_simulated_reply", None)
    return result
