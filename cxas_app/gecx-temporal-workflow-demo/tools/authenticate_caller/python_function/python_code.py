"""Caller identity and verification (ID&V) controller tool for Albion Bank."""

import logging
from typing import Any

logger = logging.getLogger(__name__)

_MAX_AUTH_ATTEMPTS = 2


def authenticate_caller(
    card_last4: str = "",
    passcode: str = "",
) -> dict:
    """Verify caller identity using debit card last 4 digits and 6-digit passcode.

    Do not speak a conversational pacing phrase before calling this tool; call
    it immediately so the caller hears only the post-tool response.

    Args:
        card_last4: Last 4 digits of the caller's Albion Bank debit card, such as "4821".
        passcode: Six-digit telephone banking passcode spoken by the caller, such as "195501".

    Returns:
        Dictionary with agent_action containing the next spoken response.
    """
    try:
        # Step 1: Load and normalize session_state variables and incoming tool arguments.
        session = dict(context.state.get("session_state") or {})
        authenticated = session.get("authenticated") is True
        customer_id = str(session.get("customer_id") or "").strip()
        customer_name = str(session.get("customer_name") or "there").strip()
        has_active_case = bool(session.get("has_active_case"))
        case_ref = str(session.get("case_ref") or "").strip()

        clean_last4 = "".join(ch for ch in str(card_last4 or "") if ch.isdigit())
        clean_passcode = "".join(ch for ch in str(passcode or "") if ch.isdigit())

        # Step 2: If the caller is already verified in this session, stage the silent
        # handoff to dispute_specialist_agent without calling the verification endpoint again.
        if authenticated:
            session["target_agent"] = "dispute_specialist_agent"
            session["pending_dispute_init"] = True
            context.state["session_state"] = session
            return {
                "agent_action": _format_verified_reply(
                    customer_name,
                    has_active_case,
                    case_ref,
                )
            }

        session["customer_id"] = customer_id
        context.state["session_state"] = session

        # Step 3: Validate that both spoken credential slots are present before counting an attempt.
        missing_prompt = _check_missing_slots(clean_last4, clean_passcode)
        if missing_prompt is not None:
            return {"agent_action": missing_prompt}

        # Step 4: Execute caller verification against the Temporal OpenAPI adapter.
        return _execute_verification(
            session=session,
            customer_id=customer_id,
            card_last4=clean_last4,
            passcode=clean_passcode,
        )
    except Exception:
        logger.exception("authenticate_caller failed")
        return _escalate_system_error(dict(context.state.get("session_state") or {}))


def _execute_verification(
    session: dict[str, Any],
    customer_id: str,
    card_last4: str,
    passcode: str,
) -> dict:
    """Execute caller verification against the Temporal /v1/dispute-workflow/verify endpoint."""
    # Step 1: Build the canonical VerifyCallerRequest payload.
    payload = _build_verify_payload(
        customer_id=customer_id,
        card_last4=card_last4,
        passcode=passcode,
    )

    # Step 2: Send the verification request to the Temporal OpenAPI adapter.
    body = _call_verify_api(payload)
    if body is None or body.get("status") == "ERROR":
        return _escalate_system_error(session)

    # Step 3: Apply the VerifyCallerResponse to session_state and return agent_action.
    return _apply_verify_response(session, body)


def _build_verify_payload(
    customer_id: str,
    card_last4: str,
    passcode: str,
) -> dict[str, Any]:
    """Construct a canonical VerifyCallerRequest dictionary."""
    return {
        "customer_id": customer_id,
        "card_last4": card_last4,
        "passcode": passcode,
    }


def _apply_verify_response(
    session: dict[str, Any],
    body: dict[str, Any],
) -> dict:
    """Handle VERIFIED and FAILED VerifyCallerResponse envelopes."""
    if body.get("status") == "VERIFIED":
        _sync_state_from_response(session, body)
        return {
            "agent_action": _format_verified_reply(
                str(session.get("customer_name") or "there"),
                bool(session.get("has_active_case")),
                str(session.get("case_ref") or ""),
            )
        }
    return _handle_failed_attempt(session)


def _sync_state_from_response(
    session: dict[str, Any],
    body: dict[str, Any],
) -> None:
    """Persist verified caller identity and handoff flags into session_state."""
    session.update(
        {
            "authenticated": True,
            "auth_attempts": 0,
            "customer_id": body["customer_id"],
            "customer_name": body["customer_name"],
            "has_active_case": bool(body.get("has_active_case")),
            "case_ref": str(body.get("active_case_ref") or ""),
            "target_agent": "dispute_specialist_agent",
            "pending_dispute_init": True,
        }
    )
    context.state["session_state"] = session


def _check_missing_slots(card_last4: str, passcode: str) -> str | None:
    """Return a spoken prompt for any missing verification fields, or None if complete."""
    missing: list[str] = []
    if len(card_last4) != 4:
        missing.append("the last four digits of your Albion Bank debit card")
    if len(passcode) != 6:
        missing.append("your six-digit telephone banking passcode")
    if not missing:
        return None
    if len(missing) == 2:
        return (
            "Welcome to Albion Bank. Before I can access your card dispute file, "
            "please share the last four digits of your debit card and your six-digit "
            "telephone banking passcode."
        )
    return f"Thank you. To complete security verification, please also tell me {missing[0]}."


def _format_verified_reply(
    customer_name: str, has_active_case: bool, active_case_ref: str
) -> str:
    """Format the post-verification acknowledgement without mentioning transfers."""
    if has_active_case and active_case_ref:
        return (
            f"Thank you for verifying your identity, {customer_name}. "
            f"I can see your active card dispute case {active_case_ref} on file."
        )
    return (
        f"Thank you for verifying your identity, {customer_name}. "
        "I have opened your debit card account."
    )


def _handle_failed_attempt(session: dict[str, Any]) -> dict:
    """Increment the failed attempt counter and lock after two failed tries."""
    attempts = int(session.get("auth_attempts") or 0) + 1
    session["auth_attempts"] = attempts
    if attempts >= _MAX_AUTH_ATTEMPTS:
        session["escalation_reason"] = "AUTH_LOCKED"
        session["call_complete"] = "true"
        context.state["session_state"] = session
        return {
            "agent_action": (
                "I am sorry, those verification details still do not match our records. "
                "For your security, I cannot proceed with card dispute changes on this call. "
                "Please check your card details or visit an Albion Bank branch with photo "
                "identification. Goodbye."
            )
        }
    context.state["session_state"] = session
    return {
        "agent_action": (
            "I am sorry, the card digits or six-digit security passcode did not match "
            "our records. Could you please double-check and repeat the last four digits "
            "of your debit card and your six-digit passcode?"
        )
    }


def _call_verify_api(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Call temporal_workflow_api_verify_caller and return the response dict or None."""
    resp = tools.temporal_workflow_api_verify_caller(payload)
    body = _safe_json(resp)
    if not resp.ok or not resp.text:
        logger.error("Verify caller HTTP error: status=%s body=%s", resp.status_code, body)
        return None
    if not body or body.get("status") == "error" or "detail" in body:
        logger.error("Verify caller error envelope: body=%s", body)
        return None
    return body


def _safe_json(resp: Any) -> dict[str, Any]:
    """Parse response JSON or wrap raw text when the body is not a JSON dict."""
    if not resp.text:
        return {}
    try:
        parsed = resp.json()
        return parsed if isinstance(parsed, dict) else {"detail": str(parsed)}
    except Exception:
        return {"detail": resp.text}


def _escalate_system_error(session: dict[str, Any]) -> dict:
    """Record a system error in session_state and return a polite spoken apology."""
    session["escalation_reason"] = "SYSTEM_ERROR"
    session["call_complete"] = "true"
    context.state["session_state"] = session
    return {
        "agent_action": (
            "I am sorry, I am unable to reach our verification service right now. "
            "Please try calling back in a few minutes. Goodbye."
        )
    }
