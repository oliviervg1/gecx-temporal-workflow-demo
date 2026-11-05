"""Generic Temporal workflow step-driver tool for Albion Bank."""

import logging
from typing import Any, Literal

logger = logging.getLogger(__name__)


def assist_card_dispute(
    operation: Literal["", "sync", "submit", "cancel"] = "",
    option_index: int = 0,
    option_text: str = "",
    confirmed: Literal["", "yes", "no"] = "",
    input_text: str = "",
) -> dict:
    """Start, resume, or advance the customer's durable Temporal card dispute workflow.

    Do not speak a conversational pacing phrase before calling this tool; call
    it immediately so the caller hears only the post-tool response.

    Args:
        operation: Generic workflow step operation ("", "sync", "submit", or "cancel").
        option_index: 1-based numbered option chosen by the caller (1 or 2),
            or 0 when not selecting an option.
        option_text: Option label or merchant name spoken by the caller when
            selecting an option, such as "Acme Electronics Ltd" or "StreamFlix Digital".
        confirmed: Caller confirmation token ("", "yes", or "no") when responding
            to a yes/no confirmation step.
        input_text: Free-text caller input when responding to an open-ended step.

    Returns:
        Dictionary with agent_action containing the next spoken response.
    """
    try:
        # Step 1: Load and normalize session_state variables and incoming tool arguments.
        session = dict(context.state.get("session_state") or {})
        authenticated = session.get("authenticated") is True
        customer_id = str(session.get("customer_id") or "").strip()
        case_ref = str(session.get("case_ref") or "").strip()
        conv_ref = str(session.get("conversation_ref") or "").strip()
        in_progress_args = dict(session.get("pending_in_progress_args") or {})

        clean_op = str(operation or "").strip()
        clean_opt_text = str(option_text or "").strip()
        clean_conf = str(confirmed or "").strip()
        clean_input = str(input_text or "").strip()
        opt_idx = int(option_index or 0)
        effective_op = _resolve_operation(
            clean_op, opt_idx, clean_opt_text, clean_conf, clean_input
        )

        # Step 2: Verify that the caller has completed ID&V in this session before
        # accessing any dispute workflow, and persist normalized correlation identifiers.
        if not authenticated:
            session["target_agent"] = "authentication_agent"
            context.state["session_state"] = session
            return {
                "agent_action": (
                    "Before I can access your card dispute file, please share the "
                    "last four digits of your debit card and your six-digit "
                    "telephone banking passcode."
                )
            }

        session["pending_dispute_init"] = False
        session["customer_id"] = customer_id
        session["conversation_ref"] = conv_ref
        context.state["session_state"] = session

        # Step 3: If the previous turn returned status="IN_PROGRESS" (meaning the
        # Temporal Update accepted the step but exceeded the synchronous wait budget),
        # and the caller has not provided new input or asked to cancel, re-send the
        # saved request payload so it attaches to the same Temporal update_id.
        if in_progress_args and not (
            clean_op == "cancel"
            or opt_idx > 0
            or clean_opt_text
            or clean_conf
            or clean_input
        ):
            return _retry_in_progress_request(session, in_progress_args)

        # Step 4: Execute the resolved workflow step operation against the Temporal adapter.
        return _execute_workflow_step(
            session=session,
            customer_id=customer_id,
            case_ref=case_ref,
            conv_ref=conv_ref,
            operation=effective_op,
            option_index=opt_idx,
            option_text=clean_opt_text,
            confirmed=clean_conf,
            input_text=clean_input,
        )
    except Exception:
        logger.exception("assist_card_dispute failed")
        return _escalate_system_error(dict(context.state.get("session_state") or {}))


def _resolve_operation(
    operation: str,
    option_index: int,
    option_text: str,
    confirmed: str,
    input_text: str,
) -> str:
    """Resolve the generic workflow operation from tool arguments."""
    # Explicit "cancel" or "sync" always takes precedence.
    if operation in ("cancel", "sync"):
        return operation
    # Any non-empty caller answer upgrades an omitted operation to "submit".
    has_input = bool(
        option_index > 0
        or option_text
        or confirmed in ("yes", "no")
        or input_text
    )
    if operation == "submit" or has_input:
        return "submit"
    # Default to "sync" when called with no operation and no caller input.
    return "sync"


def _execute_workflow_step(
    session: dict[str, Any],
    customer_id: str,
    case_ref: str,
    conv_ref: str,
    operation: str,
    option_index: int,
    option_text: str,
    confirmed: str,
    input_text: str,
) -> dict:
    """Execute a generic sync, submit, or cancel operation against the Temporal step endpoint."""
    # Step 1: If the resolved operation is "sync", fetch or start the workflow immediately.
    if operation == "sync":
        return _sync_workflow(session, customer_id, case_ref, conv_ref)

    # Step 2: If the caller sent a "submit" or "cancel" before this CXAS session has
    # synchronized with Temporal (expected_step_id is empty on cold start or callback),
    # perform an initial "sync" first so we know the active step's input_kind and options.
    synced_body: dict[str, Any] | None = None
    if not session.get("expected_step_id"):
        sync_payload = _build_step_payload(
            customer_id=customer_id,
            case_ref=case_ref,
            conv_ref=conv_ref,
            expected_step_id="",
            operation="sync",
            is_session_resume=bool(session.get("has_active_case")),
        )
        synced_body, _ = _call_step_api(sync_payload)
        if synced_body is None:
            return _escalate_system_error(session)
        if synced_body.get("status", "SUCCESS") != "SUCCESS":
            return _apply_step_response(session, synced_body, sync_payload)
        session["has_active_case"] = False
        _sync_state_from_response(session, synced_body)
        case_ref = session.get("case_ref") or case_ref
        # If the workflow is already at a non-interactive terminal step, return its summary.
        if synced_body.get("input_kind", "NONE") == "NONE":
            return {"agent_action": synced_body["customer_guidance"].strip()}

    # Step 3: Check what kind of caller input the active Temporal workflow step expects.
    # If the step expects no input ("NONE"), re-sync and return the current step summary.
    input_kind = session.get("expected_input_kind") or "NONE"
    if input_kind == "NONE":
        return _sync_workflow(session, customer_id, case_ref, conv_ref)

    # Step 4: For "submit" operations, validate and normalize the caller's answer
    # against the active step's expected_input_kind before calling the backend:
    #   - OPTION_SELECT: resolve 1-based option_index or spoken merchant option_text.
    #   - CONFIRMATION: require confirmed to be "yes" or "no".
    #   - FREE_TEXT: require non-empty input_text.
    # If the caller's utterance does not match the expected input kind (for example,
    # saying "yes" when the workflow is waiting for a transaction selection), return
    # the initial synced guidance (on cold start/resume) or the step's reprompt_guidance.
    resolved_idx = 0
    if operation == "submit":
        if input_kind == "OPTION_SELECT":
            resolved_idx = _resolve_option_index(session, option_index, option_text)
            if resolved_idx == 0:
                return {"agent_action": _fallback_guidance(session, synced_body)}
        elif input_kind == "CONFIRMATION":
            if confirmed not in ("yes", "no"):
                return {"agent_action": _fallback_guidance(session, synced_body)}
        elif input_kind == "FREE_TEXT" and not input_text:
            return {"agent_action": _fallback_guidance(session, synced_body)}

    # Step 5: Build the canonical AdvanceDisputeRequest payload, forwarding only the
    # input field that matches the active step's input_kind alongside expected_step_id.
    payload = _build_step_payload(
        customer_id=customer_id,
        case_ref=session.get("case_ref") or case_ref,
        conv_ref=conv_ref,
        expected_step_id=session.get("expected_step_id") or "",
        operation=operation,
        option_index=resolved_idx if input_kind == "OPTION_SELECT" else 0,
        confirmed=confirmed if input_kind == "CONFIRMATION" else "",
        input_text=input_text if input_kind == "FREE_TEXT" else "",
    )

    # Step 6: Send the step update to the Temporal OpenAPI adapter:
    #   - HTTP 409 (StaleStep): expected_step_id is out of date; auto-resync with Temporal.
    #   - HTTP 422 (InvalidArgument): input rejected by @submit_step.validator; re-prompt.
    body, err_code = _call_step_api(payload)
    if err_code == 409:
        return _sync_workflow(session, customer_id, case_ref, conv_ref)
    if err_code == 422:
        return {"agent_action": _reprompt(session)}
    if body is None:
        return _escalate_system_error(session)

    # Step 7: Apply the Temporal DisputeStepResponse to session_state and return agent_action.
    return _apply_step_response(session, body, payload)


def _sync_workflow(
    session: dict[str, Any],
    customer_id: str,
    case_ref: str,
    conv_ref: str,
) -> dict:
    """Synchronize session_state with the customer's active or newly started Temporal workflow."""
    # Pass is_session_resume=True only on the first sync after verify_caller
    # detected an active case.
    is_resume = bool(session.get("has_active_case"))
    payload = _build_step_payload(
        customer_id=customer_id,
        case_ref=case_ref,
        conv_ref=conv_ref,
        expected_step_id=session.get("expected_step_id") or "",
        operation="sync",
        is_session_resume=is_resume,
    )
    body, _ = _call_step_api(payload)
    if body is None:
        return _escalate_system_error(session)

    return _apply_step_response(session, body, payload)


def _build_step_payload(
    customer_id: str,
    case_ref: str,
    conv_ref: str,
    expected_step_id: str,
    operation: str,
    option_index: int = 0,
    confirmed: str = "",
    input_text: str = "",
    is_session_resume: bool = False,
) -> dict[str, Any]:
    """Construct a canonical AdvanceDisputeRequest dictionary."""
    return {
        "customer_id": customer_id,
        "case_ref": case_ref,
        "conversation_ref": conv_ref,
        "expected_step_id": expected_step_id,
        "operation": operation,
        "option_index": option_index,
        "confirmed": confirmed,
        "input_text": input_text,
        "is_session_resume": is_session_resume,
    }


def _retry_in_progress_request(
    session: dict[str, Any],
    saved_payload: dict[str, Any],
) -> dict:
    """Re-send a previously timed-out request so it joins the same Temporal update_id."""
    body, err_code = _call_step_api(saved_payload)
    # If the step already completed while the previous request was in flight, a retry
    # with the old expected_step_id returns 409; clear the cached args and sync the new step.
    if err_code == 409:
        session["pending_in_progress_args"] = {}
        context.state["session_state"] = session
        return _sync_workflow(
            session,
            str(saved_payload.get("customer_id") or session.get("customer_id") or ""),
            str(saved_payload.get("case_ref") or ""),
            str(saved_payload.get("conversation_ref") or session.get("conversation_ref") or ""),
        )
    if body is None:
        return _escalate_system_error(session)
    return _apply_step_response(session, body, saved_payload)


def _apply_step_response(
    session: dict[str, Any],
    body: dict[str, Any],
    request_payload: dict[str, Any],
) -> dict:
    """Handle SUCCESS, IN_PROGRESS, and ERROR DisputeStepResponse envelopes."""
    status = body.get("status", "SUCCESS")
    # When a Temporal Update exceeds the synchronous wait window, cache the request payload
    # inside session_state without advancing expected_step_id so the next turn can retry it.
    if status == "IN_PROGRESS":
        session["pending_in_progress_args"] = request_payload
        context.state["session_state"] = session
        return {"agent_action": body["customer_guidance"].strip()}

    # On SUCCESS or ERROR, clear any cached in-progress args and merge the new step state.
    session["pending_in_progress_args"] = {}
    session["has_active_case"] = False
    _sync_state_from_response(session, body)
    if status == "ERROR":
        session["call_complete"] = "true"
        context.state["session_state"] = session
    return {"agent_action": body["customer_guidance"].strip()}


def _resolve_option_index(
    session: dict[str, Any],
    option_index: int,
    option_text: str,
) -> int:
    """Resolve a 1-based option index from an explicit number or spoken option label."""
    options: list[str] = list(session.get("pending_workflow_options") or [])
    num_idx = 0
    # Validate numeric option_index against the 1..N bounds of the active option menu.
    if option_index:
        if not (1 <= int(option_index) <= len(options)):
            return 0
        num_idx = int(option_index)

    # Match spoken option_text (such as a merchant name) against the option labels.
    text_idx = 0
    clean_text = option_text.strip().lower()
    if clean_text:
        for idx, label in enumerate(options, start=1):
            if clean_text in label.lower():
                text_idx = idx
                break
        if text_idx == 0:
            return 0

    # If both option_index and option_text were supplied but point to different options,
    # return 0 so the caller is asked to clarify their choice.
    if num_idx and text_idx and num_idx != text_idx:
        return 0
    return num_idx or text_idx


def _sync_state_from_response(
    session: dict[str, Any],
    body: dict[str, Any],
) -> None:
    """Persist the latest Temporal DisputeStepResponse fields into session_state."""
    # Update the generic step-driver metadata and current numbered options list.
    session.update(
        {
            "workflow_id": body["workflow_id"],
            "case_ref": body["case_ref"],
            "expected_step_id": body["step_id"],
            "expected_input_kind": body.get("input_kind", "NONE"),
            "reprompt_guidance": body.get("reprompt_guidance", ""),
            "pending_workflow_options": list(body.get("options") or []),
        }
    )
    # Allow Temporal's session_vars map to set or overwrite any key in session_state.
    session.update(body.get("session_vars") or {})
    # Signal after_model_callback to close the session when the workflow reaches a terminal step.
    if body.get("end_conversation") is True:
        session["call_complete"] = "true"
    context.state["session_state"] = session


def _fallback_guidance(
    session: dict[str, Any],
    synced_body: dict[str, Any] | None,
) -> str:
    """Return initial synced guidance when uninitialized, or reprompt_guidance otherwise."""
    if synced_body is not None:
        return str(synced_body.get("customer_guidance") or "").strip()
    return _reprompt(session)


def _reprompt(session: dict[str, Any]) -> str:
    """Return the workflow step's reprompt_guidance cached in session_state."""
    guidance = str(session.get("reprompt_guidance") or "").strip()
    if guidance:
        return guidance
    return "Could you please clarify your response so I can continue with your dispute case?"


def _call_step_api(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, int]:
    """Call temporal_workflow_api_advance_dispute and return (body, error_code)."""
    resp = tools.temporal_workflow_api_advance_dispute(payload)
    body = _safe_json(resp)
    if not resp.ok or not resp.text:
        code = _infer_error_code(body, resp.status_code)
        logger.error("Advance dispute HTTP error: status=%s body=%s", code, body)
        return None, code
    if not body or body.get("status") == "error" or "detail" in body:
        code = _infer_error_code(body, resp.status_code)
        logger.error("Advance dispute error envelope: code=%s body=%s", code, body)
        return None, code
    return body, 0


def _safe_json(resp: Any) -> dict[str, Any]:
    """Parse response JSON or wrap raw text when the body is not a JSON dict."""
    if not resp.text:
        return {}
    try:
        parsed = resp.json()
        return parsed if isinstance(parsed, dict) else {"detail": str(parsed)}
    except Exception:
        return {"detail": resp.text}


def _infer_error_code(err_body: dict[str, Any], status_code: int) -> int:
    """Extract HTTP error code or infer 409/422 from platform-wrapped error text."""
    # Check for an explicit numeric error code or HTTP status code first.
    raw_code = err_body.get("errorCode") or err_body.get("error_code") or 0
    explicit = int(raw_code) if str(raw_code).isdigit() else 0
    if explicit in (409, 422):
        return explicit
    if status_code in (409, 422):
        return status_code
    # GECX OpenAPI error envelopes map HTTP 409/422 to gRPC status strings in errorCode.
    code_str = str(raw_code).upper()
    if code_str in ("STALE_STEP", "ABORTED", "ALREADY_EXISTS"):
        return 409
    if code_str in ("INVALID_ARGUMENT", "INVALID_OPTION", "INVALID_INPUT"):
        return 422
    # Fallback: check the error message text for 409 stale-step or 422 validation phrases.
    msg = (
        f"{err_body.get('error', '')} "
        f"{err_body.get('detail', '')} "
        f"{err_body.get('customer_guidance', '')}"
    ).lower()
    if (
        "409" in msg
        or "stale step" in msg
        or "not valid at step" in msg
        or "not valid at non-interactive step" in msg
    ):
        return 409
    if (
        "422" in msg
        or "invalid option_index" in msg
        or "invalid confirmed" in msg
        or "invalid input_text" in msg
    ):
        return 422
    return explicit or status_code or 500


def _escalate_system_error(session: dict[str, Any]) -> dict:
    """Record a system error in session_state and return a polite spoken apology."""
    session["escalation_reason"] = "SYSTEM_ERROR"
    session["call_complete"] = "true"
    context.state["session_state"] = session
    return {
        "agent_action": (
            "I am sorry, I am unable to reach our card dispute workflow service right "
            "now. Any existing security protection on your card remains in place, and "
            "we will send an SMS update shortly. Goodbye."
        )
    }
