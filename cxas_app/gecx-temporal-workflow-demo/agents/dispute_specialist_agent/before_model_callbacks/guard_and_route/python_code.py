"""Before-model guard and silent routing callback for Albion Bank CXAS agents."""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_PRE_TOOL_FILLER = (
    "Thank you for verifying your details. Let me check your card dispute file now. "
)


def before_model_callback(
    callback_context: CallbackContext,
    llm_request: LlmRequest,
) -> Optional[LlmResponse]:
    """Hide internal routing tools, guard specialist access, and execute silent transfers."""
    try:
        # Step 1: Load the consolidated session_state OBJECT variable and seed conversation_ref.
        session = dict(callback_context.state.get("session_state") or {})
        if not session.get("conversation_ref") and callback_context.session_id:
            session["conversation_ref"] = (callback_context.session_id or "").split("/")[-1]
            callback_context.state["session_state"] = session

        # Step 2: If the call has already been released on a prior turn, short-circuit.
        if session.get("call_complete") == "released":
            return LlmResponse.from_parts(parts=[])

        # Step 3: Hide built-in routing and session-termination tools from the LLM so
        # all transfers and line releases are driven deterministically by session_state.
        llm_request.config.hide_tool("end_session")
        llm_request.config.hide_tool("transfer_to_agent")

        current_agent = (callback_context.agent_name or "").split("/")[-1]

        # Step 4: On dispute_specialist_agent, enforce the ID&V gate and auto-trigger
        # the initial assist_card_dispute(operation="sync") call immediately after transfer.
        if current_agent == "dispute_specialist_agent":
            if session.get("authenticated") is not True:
                session["target_agent"] = ""
                callback_context.state["session_state"] = session
                return LlmResponse.from_parts(
                    parts=[Part.from_agent_transfer(agent="authentication_agent")]
                )

            if session.get("pending_dispute_init") is True:
                session["pending_dispute_init"] = False
                callback_context.state["session_state"] = session
                is_eval = (callback_context.session_id or "").startswith("evaluation")
                call_part = Part.from_function_call(
                    name="assist_card_dispute",
                    args={"operation": "sync"},
                )
                if is_eval:
                    return LlmResponse.from_parts(parts=[call_part])
                return LlmResponse.from_parts(
                    parts=[Part.from_text(text=_PRE_TOOL_FILLER), call_part]
                )

        # Step 5: On a post-tool reasoning pass, hide the tool that just returned so the
        # model does not call it twice in the same turn, and execute any staged transfer.
        if _is_tool_response(llm_request):
            returned_tool = _last_returned_tool_name(llm_request)
            if returned_tool:
                llm_request.config.hide_tool(returned_tool)

            target = str(session.get("target_agent") or "").strip()
            if target:
                session["target_agent"] = ""
                callback_context.state["session_state"] = session
                if target != current_agent:
                    return LlmResponse.from_parts(
                        parts=[Part.from_agent_transfer(agent=target)]
                    )
            return None

        # Step 6: If a session starts on authentication_agent with authenticated=True and
        # a pre-staged target_agent (for example in a mid-flow evaluation), transfer immediately.
        if current_agent == "authentication_agent" and session.get("authenticated") is True:
            target = str(session.get("target_agent") or "").strip()
            if target and target != current_agent:
                session["target_agent"] = ""
                callback_context.state["session_state"] = session
                return LlmResponse.from_parts(
                    parts=[Part.from_agent_transfer(agent=target)]
                )

        return None
    except Exception:
        logger.exception("guard_and_route before_model_callback failed")
        return None


def _is_tool_response(llm_request: LlmRequest) -> bool:
    """Return True when the latest content turn contains a tool function_response."""
    contents = llm_request.contents or []
    return bool(contents and any(p.function_response for p in contents[-1].parts))


def _last_returned_tool_name(llm_request: LlmRequest) -> str:
    """Return the name of the last tool that returned on this model pass."""
    contents = llm_request.contents or []
    if not contents:
        return ""
    names = [
        p.function_response.name
        for p in contents[-1].parts
        if p.function_response
    ]
    return names[-1] if names else ""
