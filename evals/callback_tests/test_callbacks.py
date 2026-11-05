"""Unit tests for before_model_callback and after_model_callback chains."""

from evals.conftest import (
    FakeContent,
    FakeFunctionCall,
    FakeFunctionResponse,
    FakeLlmRequest,
    FakeLlmResponse,
    FakePart,
)


def test_before_model_callback_hides_tools_and_executes_silent_transfer(
    load_callbacks,
) -> None:
    """Verify guard_and_route hides tools and executes post-tool silent transfer."""
    before_cb, _, ctx = load_callbacks(
        initial_state={
            "session_state": {
                "authenticated": True,
                "target_agent": "dispute_specialist_agent",
                "pending_dispute_init": True,
            }
        },
        agent_name="authentication_agent",
    )
    req = FakeLlmRequest(
        contents=[
            FakeContent(
                parts=[
                    FakePart(
                        function_response=FakeFunctionResponse(
                            name="authenticate_caller",
                            response={"agent_action": "Verified."},
                        )
                    )
                ]
            )
        ]
    )
    resp = before_cb(ctx, req)
    assert "end_session" in req.config.hidden_tools
    assert "transfer_to_agent" in req.config.hidden_tools
    assert "authenticate_caller" in req.config.hidden_tools
    assert resp is not None
    assert len(resp.content.parts) == 1
    assert resp.content.parts[0].agent_transfer == "dispute_specialist_agent"
    assert ctx.state["session_state"]["target_agent"] == ""


def test_dispute_specialist_entry_emits_filler_even_with_trailing_function_response(
    load_callbacks,
) -> None:
    """Verify specialist fires pending_dispute_init even after auth function_response."""
    # 1. Unauthenticated access bounces back to authentication_agent
    before_unauth, _, ctx_unauth = load_callbacks(
        initial_state={"session_state": {"authenticated": False}},
        agent_name="dispute_specialist_agent",
    )
    resp_unauth = before_unauth(ctx_unauth, FakeLlmRequest())
    assert resp_unauth is not None
    assert resp_unauth.content.parts[0].agent_transfer == "authentication_agent"

    # 2. Live session after transfer (where last content is authenticate_caller function_response)
    before_live, _, ctx_live = load_callbacks(
        initial_state={
            "session_state": {"authenticated": True, "pending_dispute_init": True}
        },
        agent_name="dispute_specialist_agent",
        session_id="session-live-001",
    )
    req_after_transfer = FakeLlmRequest(
        contents=[
            FakeContent(
                parts=[
                    FakePart(
                        function_response=FakeFunctionResponse(
                            name="authenticate_caller",
                            response={"agent_action": "Verified."},
                        )
                    )
                ]
            )
        ]
    )
    resp_live = before_live(ctx_live, req_after_transfer)
    assert resp_live is not None
    assert len(resp_live.content.parts) == 2
    assert "Thank you for verifying your details" in (
        resp_live.content.parts[0].text or ""
    )
    assert resp_live.content.parts[1].function_call is not None
    assert resp_live.content.parts[1].function_call.name == "assist_card_dispute"
    assert resp_live.content.parts[1].function_call.args == {"operation": "sync"}
    assert ctx_live.state["session_state"]["pending_dispute_init"] is False

    # 3. Evaluation session emits only assist_card_dispute(operation="sync")
    before_eval, _, ctx_eval = load_callbacks(
        initial_state={
            "session_state": {"authenticated": True, "pending_dispute_init": True}
        },
        agent_name="dispute_specialist_agent",
        session_id="evaluation-run-001",
    )
    resp_eval = before_eval(ctx_eval, req_after_transfer)
    assert resp_eval is not None
    assert len(resp_eval.content.parts) == 1
    assert resp_eval.content.parts[0].function_call is not None
    assert resp_eval.content.parts[0].function_call.name == "assist_card_dispute"
    assert resp_eval.content.parts[0].function_call.args == {"operation": "sync"}


def test_after_model_callback_passes_through_tool_calls_and_ends_terminal_session(
    load_callbacks,
) -> None:
    """Verify after_model_callback lets controller tool calls execute and appends end_session."""
    _, after_cb, ctx = load_callbacks(
        initial_state={"session_state": {"authenticated": True}},
        agent_name="authentication_agent",
    )
    call_resp = FakeLlmResponse.from_parts(
        parts=[
            FakePart(
                function_call=FakeFunctionCall(
                    name="authenticate_caller",
                    args={
                        "card_last4": "4821",
                        "passcode": "195501",
                    },
                    id="fc-call-99",
                )
            )
        ]
    )
    # Controller tool calls pass through untouched without speculative arg repair
    assert after_cb(ctx, call_resp) is None

    # Terminal turn appends end_session and latches call_complete="released"
    ctx.state["session_state"]["call_complete"] = "true"
    ctx.state["session_state"]["escalation_reason"] = ""
    speech_resp = FakeLlmResponse.from_parts(
        parts=[FakePart.from_text(text="Your dispute is submitted. Goodbye.")]
    )
    closed = after_cb(ctx, speech_resp)
    assert closed is not None
    assert len(closed.content.parts) == 2
    assert closed.content.parts[1].end_session == {
        "reason": "CALL_COMPLETED",
        "escalated": False,
    }
    assert ctx.state["session_state"]["call_complete"] == "released"
