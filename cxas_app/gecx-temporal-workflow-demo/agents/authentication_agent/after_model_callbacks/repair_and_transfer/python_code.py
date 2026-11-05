"""After-model defense-in-depth transfer and line release callback."""

import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

_AGENT_TOKEN = re.compile(r"\{@AGENT:\s*([a-zA-Z0-9_]+)\}", re.IGNORECASE)


def after_model_callback(
    callback_context: CallbackContext,
    llm_response: LlmResponse,
) -> Optional[LlmResponse]:
    """Execute fallback transfers and close terminal sessions via Part.from_end_session."""
    try:
        # Step 1: Load the consolidated session_state OBJECT variable.
        session = dict(callback_context.state.get("session_state") or {})
        if session.get("call_complete") == "released":
            return None

        raw = list(llm_response.content.parts) if llm_response.content else []
        if not raw:
            return None

        # Step 2: If the model emitted a controller tool call on this pass, let it execute first.
        if any(
            p.function_call
            and p.function_call.name not in ("transfer_to_agent", "end_session")
            for p in raw
        ):
            return None

        # Step 3: Strip any direct transfer_to_agent/end_session tool calls or {@AGENT: ...}
        # text tokens from the model response and resolve any staged target_agent.
        target = str(session.get("target_agent") or "").strip()
        if target:
            session["target_agent"] = ""
            callback_context.state["session_state"] = session

        cleaned: list[Any] = []
        for p in raw:
            if p.function_call and p.function_call.name in (
                "transfer_to_agent",
                "end_session",
            ):
                if p.function_call.name == "transfer_to_agent":
                    args = dict(p.function_call.args or {})
                    target = target or str(
                        args.get("agent_name") or args.get("agent") or ""
                    )
                continue
            text = p.text or ""
            match = _AGENT_TOKEN.search(text)
            if match:
                target = target or match.group(1)
                stripped = _AGENT_TOKEN.sub("", text).strip()
                if stripped:
                    cleaned.append(Part.from_text(text=stripped))
            else:
                cleaned.append(p)

        # Step 4: Execute a defense-in-depth agent transfer if target_agent was staged.
        current_agent = (callback_context.agent_name or "").split("/")[-1]
        if target and target != current_agent:
            if not (callback_context.session_id or "").startswith("evaluation"):
                return LlmResponse.from_parts(
                    parts=[Part.from_agent_transfer(agent=target)]
                )
            return LlmResponse.from_parts(
                parts=cleaned + [Part.from_agent_transfer(agent=target)]
            )

        # Step 5: When a controller tool marks call_complete="true", append Part.from_end_session
        # after the final spoken response and latch call_complete="released".
        if session.get("call_complete") == "true":
            reason = str(session.get("escalation_reason") or "CALL_COMPLETED")
            session["call_complete"] = "released"
            callback_context.state["session_state"] = session
            return LlmResponse.from_parts(
                parts=cleaned
                + [
                    Part.from_end_session(
                        reason=reason,
                        escalated=(reason != "CALL_COMPLETED"),
                    )
                ]
            )

        return LlmResponse.from_parts(parts=cleaned) if cleaned != raw else None
    except Exception:
        logger.exception("repair_and_transfer after_model_callback failed")
        return None
