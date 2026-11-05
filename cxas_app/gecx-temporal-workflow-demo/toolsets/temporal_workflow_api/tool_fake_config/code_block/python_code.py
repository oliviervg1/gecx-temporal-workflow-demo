"""Deterministic tool fake for the Albion Bank Temporal Dispute Workflow OpenAPI toolset.

Produces responses that validate identically against VerifyCallerResponse and
DisputeStepResponse in temporal_service/models.py.
"""

import hashlib
from typing import Any

_RESUME_FIXTURE_CUSTOMERS: frozenset[str] = frozenset(
    {
        "customer-claire-resume",
        "customer-claire-decline",
        "customer-claire-stale",
    }
)

_CUSTOMERS: dict[str, dict[str, Any]] = {
    cid: {
        "customer_id": cid,
        "customer_name": "Claire Vance",
        "card_last4": "4821",
        "passcode": "195501",
        "default_case_ref": "DSP-2026-8841",
    }
    for cid in (
        "customer-claire-bristol",
        "customer-claire-new",
        "customer-claire-resume",
        "customer-claire-decline",
        "customer-claire-stale",
    )
}

_TRANSACTIONS: list[dict[str, Any]] = [
    {
        "tx_id": "TX-90412",
        "merchant_name": "Acme Electronics Ltd",
        "amount_gbp": 142.50,
        "date": "2 Nov 2026",
        "category": "Retail Electronics",
        "scheme_reason_code": "13.1 — Merchandise / Services Not Received",
        "label": "£142.50 at Acme Electronics Ltd on 2 Nov 2026",
    },
    {
        "tx_id": "TX-90388",
        "merchant_name": "StreamFlix Digital",
        "amount_gbp": 29.99,
        "date": "1 Nov 2026",
        "category": "Digital Subscription",
        "scheme_reason_code": "13.2 — Cancelled Recurring Transaction",
        "label": "£29.99 at StreamFlix Digital on 1 Nov 2026",
    },
]

_FAKE_WORKFLOWS: dict[str, dict[str, Any]] = {}
_ALLOWED_STEP_KEYS: frozenset[str] = frozenset(
    {
        "customer_id",
        "case_ref",
        "conversation_ref",
        "expected_step_id",
        "operation",
        "option_index",
        "confirmed",
        "input_text",
        "is_session_resume",
    }
)


def fake_tool_call(
    tool: Any,
    input: dict[str, Any],
    callback_context: Any,
) -> dict[str, Any] | None:
    """Entry point invoked by CX Agent Studio when toolFakeConfig.enableFakeMode is True."""
    del callback_context
    if tool.name == "temporal_workflow_api_verify_caller":
        return _handle_verify(input)
    if tool.name == "temporal_workflow_api_advance_dispute":
        return _handle_step(input)
    return {"detail": f"Unknown tool: {tool.name}", "errorCode": 400}


def _seed_resume_customer(cid: str) -> None:
    """Seeds a single resume-fixture customer at CONFIRM_CARD_REPLACEMENT."""
    record = _CUSTOMERS[cid]
    _FAKE_WORKFLOWS[cid] = _build_confirm_replacement_state(
        cid,
        record,
        "DSP-2026-8841",
        _TRANSACTIONS[0],
        "conv-prior:select_transaction:seed0001",
    )


def reset_fake_workflows() -> None:
    """Clear in-memory workflow state and seed all cross-session resume fixtures."""
    _FAKE_WORKFLOWS.clear()
    for cid in _RESUME_FIXTURE_CUSTOMERS:
        _seed_resume_customer(cid)


def _compute_update_id(payload: dict[str, Any]) -> str:
    operation = str(payload.get("operation") or "sync")
    opt_idx = int(payload.get("option_index") or 0)
    confirmed = str(payload.get("confirmed") or "")
    input_text = str(payload.get("input_text") or "")
    canonical = f"{operation}|{opt_idx}|{confirmed}|{input_text}"
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]
    step_tok = str(payload.get("expected_step_id") or "") or "init"
    conv_tok = str(payload.get("conversation_ref") or "") or "conv"
    return f"{conv_tok}:{step_tok}:{digest}"


def _apply_resume_greeting(
    state: dict[str, Any],
    customer_name: str,
    is_session_resume: bool,
) -> dict[str, Any]:
    """Prepend cross-session resume greeting when syncing an active RUNNING workflow."""
    res = dict(state)
    if not is_session_resume or res.get("status") != "SUCCESS":
        return res
    guidance = str(res.get("customer_guidance") or "")
    if guidance.startswith("Welcome back,"):
        return res
    name = customer_name or "there"
    case_ref = res.get("case_ref", "DSP-2026-8841")
    res["customer_guidance"] = (
        f"Welcome back, {name}. Resuming active dispute case {case_ref}. {guidance}"
    )
    return res


def _handle_verify(payload: dict[str, Any]) -> dict[str, Any]:
    """Simulate POST /v1/dispute-workflow/verify matching VerifyCallerResponse."""
    if "customer-claire-resume" not in _FAKE_WORKFLOWS:
        reset_fake_workflows()

    req_cid = str(payload.get("customer_id") or "customer-claire-bristol").strip()
    record = _CUSTOMERS.get(req_cid, _CUSTOMERS["customer-claire-bristol"])
    cid = record["customer_id"]
    card_last4 = "".join(ch for ch in str(payload.get("card_last4", "")) if ch.isdigit())
    passcode = "".join(ch for ch in str(payload.get("passcode", "")) if ch.isdigit())

    if not card_last4 or not passcode:
        return {
            "status": "INVALID_CREDENTIALS",
            "customer_id": cid,
            "customer_name": "",
            "has_active_case": False,
            "active_case_ref": "",
            "message": "Please provide the last four digits of the card and six-digit passcode.",
        }

    if card_last4 == record["card_last4"] and passcode == record["passcode"]:
        if cid in ("customer-claire-bristol", "customer-claire-new"):
            _FAKE_WORKFLOWS.pop(cid, None)
        elif cid in _RESUME_FIXTURE_CUSTOMERS:
            _seed_resume_customer(cid)
        existing = _FAKE_WORKFLOWS.get(cid)
        is_running = bool(
            existing
            and existing["workflow_status"]
            in (
                "SELECT_TRANSACTION",
                "CONFIRM_CARD_REPLACEMENT",
                "AWAITING_MERCHANT_REPRESENTMENT",
            )
        )
        case_ref = existing["case_ref"] if is_running else ""
        return {
            "status": "VERIFIED",
            "customer_id": cid,
            "customer_name": record["customer_name"],
            "has_active_case": is_running,
            "active_case_ref": case_ref,
            "message": (
                f"Identity verified for {record['customer_name']}."
                + (
                    f" Active dispute case {case_ref} found."
                    if is_running
                    else " Ready to start card dispute review."
                )
            ),
        }

    return {
        "status": "INVALID_CREDENTIALS",
        "customer_id": cid,
        "customer_name": "",
        "has_active_case": False,
        "active_case_ref": "",
        "message": "The card digits or six-digit security passcode did not match our records.",
    }


def _handle_step(payload: dict[str, Any]) -> dict[str, Any]:
    """Simulate POST /v1/dispute-workflow/step matching DisputeStepResponse and validator errors."""
    if "customer-claire-resume" not in _FAKE_WORKFLOWS:
        reset_fake_workflows()

    unknown_keys = set(payload.keys()) - _ALLOWED_STEP_KEYS
    if unknown_keys:
        return {
            "detail": f"Extra inputs are not permitted: {sorted(unknown_keys)}",
            "errorCode": 422,
        }

    req_cid = str(payload.get("customer_id") or "customer-claire-bristol").strip()
    record = _CUSTOMERS.get(req_cid, _CUSTOMERS["customer-claire-bristol"])
    cid = record["customer_id"]
    case_ref = str(payload.get("case_ref") or "").strip() or record["default_case_ref"]
    operation = str(payload.get("operation") or "sync").strip()
    expected_step_id = str(payload.get("expected_step_id") or "").strip()
    is_session_resume = bool(payload.get("is_session_resume", False))
    update_id = _compute_update_id(payload)

    conv_ref = str(payload.get("conversation_ref") or "").strip()
    force_fixture_reset = (
        (conv_ref == "conv-tool-test" and operation != "sync")
        or (operation == "submit" and expected_step_id == "select_transaction")
    )
    if cid in _RESUME_FIXTURE_CUSTOMERS and (
        cid not in _FAKE_WORKFLOWS
        or _FAKE_WORKFLOWS[cid]["workflow_status"]
        in ("RESOLVED_CREDITED", "CANCELLED_COMPENSATED", "ABANDONED_COMPENSATED")
        or force_fixture_reset
    ):
        _seed_resume_customer(cid)

    state = _FAKE_WORKFLOWS.get(cid)
    if state is not None and state["workflow_status"] in (
        "RESOLVED_CREDITED",
        "CANCELLED_COMPENSATED",
        "ABANDONED_COMPENSATED",
    ):
        if operation == "sync":
            res = dict(state)
            res["update_id"] = update_id
            return res
        closed = dict(state)
        closed["status"] = "ERROR"
        closed["error_code"] = "CASE_CLOSED"
        closed["end_conversation"] = True
        closed["update_id"] = update_id
        closed["customer_guidance"] = (
            f"Dispute case {case_ref} is already closed "
            f"({state['workflow_status']}) and cannot be modified."
        )
        return closed

    if operation == "sync":
        if state is None:
            state = _build_select_transaction_state(cid, record, case_ref, update_id)
            _FAKE_WORKFLOWS[cid] = state
            return dict(state)
        res = dict(state)
        res["update_id"] = update_id
        return _apply_resume_greeting(res, record["customer_name"], is_session_resume)

    if state is None:
        if cid == "customer-claire-bristol" and expected_step_id in (
            "select_transaction",
            "confirm_card_replacement",
            "stale_step_0",
        ):
            if expected_step_id in ("confirm_card_replacement", "stale_step_0"):
                state = _build_confirm_replacement_state(
                    cid, record, case_ref, _TRANSACTIONS[0], update_id
                )
            else:
                state = _build_select_transaction_state(cid, record, case_ref, update_id)
            _FAKE_WORKFLOWS[cid] = state
        else:
            return {
                "detail": (
                    f"Stale step submission: workflow is still initializing Step 1; "
                    f"operation '{operation}' is not valid before initial sync."
                ),
                "errorCode": 409,
            }
    elif (
        cid == "customer-claire-bristol"
        and expected_step_id in ("confirm_card_replacement", "stale_step_0")
        and state["step_id"] == "select_transaction"
    ):
        state = _build_confirm_replacement_state(
            cid, record, case_ref, _TRANSACTIONS[0], update_id
        )
        _FAKE_WORKFLOWS[cid] = state

    if not expected_step_id:
        return {
            "detail": (
                f"Stale step submission: expected_step_id is required for operation '{operation}' "
                f"(current step is '{state['step_id']}')."
            ),
            "errorCode": 409,
        }

    if expected_step_id != state["step_id"]:
        return {
            "detail": (
                f"Stale step submission: expected '{state['step_id']}', "
                f"got '{expected_step_id}'."
            ),
            "errorCode": 409,
        }

    if state["input_kind"] == "NONE":
        return {
            "detail": (
                f"Operation '{operation}' is not valid at non-interactive step "
                f"'{state['step_id']}'."
            ),
            "errorCode": 409,
        }

    if operation == "cancel":
        return _build_cancelled_state(cid, record, state, update_id)

    if state["input_kind"] == "OPTION_SELECT":
        opt_idx = int(payload.get("option_index") or 0)
        max_opts = len(state.get("options") or [])
        if opt_idx < 1 or opt_idx > max_opts:
            return {
                "detail": f"Invalid option_index={opt_idx}; expected 1..{max_opts}.",
                "errorCode": 422,
            }
        next_state = _build_confirm_replacement_state(
            cid, record, case_ref, _TRANSACTIONS[opt_idx - 1], update_id
        )
        _FAKE_WORKFLOWS[cid] = next_state
        return dict(next_state)

    if state["input_kind"] == "CONFIRMATION":
        confirmed = str(payload.get("confirmed") or "").strip()
        if confirmed not in ("yes", "no"):
            return {
                "detail": f"Invalid confirmed='{confirmed}'; expected 'yes' or 'no'.",
                "errorCode": 422,
            }
        if confirmed == "no":
            return _build_cancelled_state(cid, record, state, update_id)
        next_state = _build_awaiting_representment_state(
            cid, record, case_ref, state["selected_transaction"], update_id
        )
        _FAKE_WORKFLOWS[cid] = next_state
        return dict(next_state)

    if state["input_kind"] == "FREE_TEXT":
        if not str(payload.get("input_text") or "").strip():
            return {
                "detail": "Invalid input_text: non-empty text is required.",
                "errorCode": 422,
            }

    return {
        "detail": f"Unsupported input_kind='{state['input_kind']}'.",
        "errorCode": 422,
    }


def _build_select_transaction_state(
    cid: str,
    record: dict[str, Any],
    case_ref: str,
    update_id: str,
) -> dict[str, Any]:
    options = [tx["label"] for tx in _TRANSACTIONS]
    menu_choices = ", or ".join(
        f"{idx + 1} for {label}" for idx, label in enumerate(options)
    )
    return {
        "status": "SUCCESS",
        "error_code": "",
        "workflow_id": f"dispute:{cid}:{case_ref}",
        "case_ref": case_ref,
        "workflow_status": "SELECT_TRANSACTION",
        "step_id": "select_transaction",
        "step_seq": 1,
        "input_kind": "OPTION_SELECT",
        "update_id": update_id,
        "context_summary": (
            f"Case {case_ref} opened for card ending {record['card_last4']}. "
            f"Found {len(options)} recent settled transactions eligible for review."
        ),
        "customer_guidance": (
            f"I have opened dispute case {case_ref} for your card ending "
            f"{record['card_last4']}. Which recent transaction would you like to dispute: "
            f"{menu_choices}?"
        ),
        "reprompt_guidance": (
            f"I could not match that selection. Please choose {menu_choices}."
        ),
        "end_conversation": False,
        "session_vars": {
            "workflow_status": "SELECT_TRANSACTION",
            "card_hold_active": False,
            "provisional_credit_gbp": "0.00",
        },
        "options": options,
        "selected_transaction": {},
        "provisional_credit_gbp": 0.0,
        "card_hold_active": False,
        "saga_compensated": False,
        "outbound_receipt_sent": False,
        "next_step_estimated_ms": 900,
    }


def _build_confirm_replacement_state(
    cid: str,
    record: dict[str, Any],
    case_ref: str,
    tx: dict[str, Any],
    update_id: str,
) -> dict[str, Any]:
    amount = tx["amount_gbp"]
    tx_label = tx["label"]
    return {
        "status": "SUCCESS",
        "error_code": "",
        "workflow_id": f"dispute:{cid}:{case_ref}",
        "case_ref": case_ref,
        "workflow_status": "CONFIRM_CARD_REPLACEMENT",
        "step_id": "confirm_card_replacement",
        "step_seq": 2,
        "input_kind": "CONFIRMATION",
        "update_id": update_id,
        "context_summary": (
            f"Verified settlement for {tx['merchant_name']} (£{amount:.2f} on {tx['date']}), "
            f"matched Visa scheme rule ({tx['scheme_reason_code']}), and confirmed zero "
            "duplicate claims. Temporary security hold placed on card ending "
            f"{record['card_last4']}. Provisional credit of £{amount:.2f} ready."
        ),
        "customer_guidance": (
            f"Your selected transaction ({tx_label}) qualifies for an immediate "
            f"£{amount:.2f} provisional credit under case {case_ref}, and I have "
            f"placed a temporary security hold on card ending {record['card_last4']}. Would "
            "you like me to permanently block that card, order a replacement card ending "
            "8914 arriving in 3 to 5 UK working days, and submit the chargeback claim?"
        ),
        "reprompt_guidance": (
            "Before I submit the chargeback and issue your provisional credit, "
            "please confirm with yes or no whether you would like me to permanently "
            "block your card, order a replacement, and file the claim."
        ),
        "end_conversation": False,
        "session_vars": {
            "workflow_status": "CONFIRM_CARD_REPLACEMENT",
            "card_hold_active": True,
            "provisional_credit_gbp": "0.00",
        },
        "options": [],
        "selected_transaction": dict(tx),
        "provisional_credit_gbp": 0.0,
        "card_hold_active": True,
        "saga_compensated": False,
        "outbound_receipt_sent": False,
        "next_step_estimated_ms": 1100,
    }


def _build_awaiting_representment_state(
    cid: str,
    record: dict[str, Any],
    case_ref: str,
    tx: dict[str, Any],
    update_id: str,
) -> dict[str, Any]:
    amount = float(tx["amount_gbp"])
    receipt_summary = (
        f"Case {case_ref}: £{amount:.2f} provisional credit issued for "
        f"{tx['merchant_name']}; replacement card ending 8914 dispatched."
    )
    return {
        "status": "SUCCESS",
        "error_code": "",
        "workflow_id": f"dispute:{cid}:{case_ref}",
        "case_ref": case_ref,
        "workflow_status": "AWAITING_MERCHANT_REPRESENTMENT",
        "step_id": "awaiting_merchant_representment",
        "step_seq": 3,
        "input_kind": "NONE",
        "update_id": update_id,
        "context_summary": (
            f"{receipt_summary} SMS/email confirmation sent. Case is now in the 7-day Visa "
            "merchant representment window."
        ),
        "customer_guidance": (
            f"I have applied £{amount:.2f} in provisional credit to your account, "
            f"permanently blocked card ending {record['card_last4']}, and ordered your "
            f"replacement card ending 8914 arriving in 3 to 5 UK working days. Dispute "
            f"case {case_ref} is now in the 7-day Visa merchant representment window, and "
            "we have sent your confirmation receipt."
        ),
        "reprompt_guidance": "",
        "end_conversation": True,
        "session_vars": {
            "workflow_status": "AWAITING_MERCHANT_REPRESENTMENT",
            "card_hold_active": False,
            "provisional_credit_gbp": f"{amount:.2f}",
        },
        "options": [],
        "selected_transaction": dict(tx),
        "provisional_credit_gbp": amount,
        "card_hold_active": False,
        "saga_compensated": False,
        "outbound_receipt_sent": True,
        "next_step_estimated_ms": 400,
    }


def _build_cancelled_state(
    cid: str,
    record: dict[str, Any],
    prior_state: dict[str, Any],
    update_id: str,
) -> dict[str, Any]:
    had_hold = bool(prior_state.get("card_hold_active"))
    case_ref = prior_state["case_ref"]
    hold_note = (
        f"Saga compensation released the temporary hold on card ending {record['card_last4']}."
        if had_hold
        else f"Card ending {record['card_last4']} remains active with no changes."
    )
    next_state = {
        "status": "SUCCESS",
        "error_code": "",
        "workflow_id": prior_state["workflow_id"],
        "case_ref": case_ref,
        "workflow_status": "CANCELLED_COMPENSATED",
        "step_id": "cancelled_compensated",
        "step_seq": int(prior_state["step_seq"]) + 1,
        "input_kind": "NONE",
        "update_id": update_id,
        "context_summary": f"Dispute case {case_ref} closed (CUSTOMER_CANCELLED). {hold_note}",
        "customer_guidance": (
            f"I have cancelled dispute case {case_ref}. Card ending "
            f"{record['card_last4']} remains active with any temporary hold removed."
        ),
        "reprompt_guidance": "",
        "end_conversation": True,
        "session_vars": {
            "workflow_status": "CANCELLED_COMPENSATED",
            "card_hold_active": False,
            "provisional_credit_gbp": "0.00",
        },
        "options": [],
        "selected_transaction": dict(prior_state.get("selected_transaction", {})),
        "provisional_credit_gbp": 0.0,
        "card_hold_active": False,
        "saga_compensated": had_hold,
        "outbound_receipt_sent": False,
        "next_step_estimated_ms": 300,
    }
    _FAKE_WORKFLOWS[cid] = next_state
    return dict(next_state)
