"""Unit, state-matrix, and Pydantic schema parity tests for CXAS controller tools."""

import re

from evals.conftest import FakeExternalResponse
from temporal_service.models import DisputeStepResponse, VerifyCallerResponse

_FORBIDDEN_WORDS = re.compile(
    r"\b(transfer|transferring|connecting|handoff)\b", re.IGNORECASE
)


def _assert_clean_action(result: dict) -> str:
    assert list(result.keys()) == ["agent_action"]
    text = result["agent_action"]
    assert isinstance(text, str) and text.strip()
    assert not _FORBIDDEN_WORDS.search(text), f"Forbidden word in agent_action: {text}"
    return text


def test_fake_toolset_responses_validate_against_adapter_pydantic_models(
    load_controllers,
) -> None:
    """Verify every response emitted by toolFakeConfig validates against Step 1 Pydantic models."""
    auth_fn, dispute_fn, _, tools_mock = load_controllers()
    auth_fn(card_last4="4821", passcode="195501")
    dispute_fn(operation="sync")
    dispute_fn(operation="submit", option_index=1)
    dispute_fn(operation="submit", confirmed="yes")

    assert len(tools_mock.verify_calls) == 1
    assert len(tools_mock.step_calls) == 3

    # Replay calls against fake_tool_call and validate with Pydantic
    fake_mod = tools_mock.fake_mod
    fake_mod.reset_fake_workflows()
    v_res = fake_mod.fake_tool_call(
        type("T", (), {"name": "temporal_workflow_api_verify_caller"})(),
        {"customer_id": "customer-claire-bristol", "card_last4": "4821", "passcode": "195501"},
        None,
    )
    VerifyCallerResponse.model_validate(v_res)

    s1 = fake_mod.fake_tool_call(
        type("T", (), {"name": "temporal_workflow_api_advance_dispute"})(),
        {"customer_id": "customer-claire-bristol", "operation": "sync"},
        None,
    )
    DisputeStepResponse.model_validate(s1)

    s2 = fake_mod.fake_tool_call(
        type("T", (), {"name": "temporal_workflow_api_advance_dispute"})(),
        {
            "customer_id": "customer-claire-bristol",
            "operation": "submit",
            "expected_step_id": "select_transaction",
            "option_index": 1,
        },
        None,
    )
    DisputeStepResponse.model_validate(s2)

    s3 = fake_mod.fake_tool_call(
        type("T", (), {"name": "temporal_workflow_api_advance_dispute"})(),
        {
            "customer_id": "customer-claire-bristol",
            "operation": "submit",
            "expected_step_id": "confirm_card_replacement",
            "confirmed": "yes",
        },
        None,
    )
    DisputeStepResponse.model_validate(s3)


def test_authenticate_caller_slot_collection_and_success(load_controllers) -> None:
    """Verify missing slot prompts do not burn attempts and valid credentials verify."""
    auth_fn, _, ctx, tools_mock = load_controllers()

    r0 = auth_fn()
    text0 = _assert_clean_action(r0)
    assert "last four digits" in text0
    assert "six-digit" in text0
    assert ctx.state["session_state"].get("auth_attempts", 0) == 0

    r_partial = auth_fn(card_last4="4821")
    text_partial = _assert_clean_action(r_partial)
    assert "six-digit telephone banking passcode" in text_partial
    assert ctx.state["session_state"].get("auth_attempts", 0) == 0

    r_ok = auth_fn(card_last4="4821", passcode="195501")
    text_ok = _assert_clean_action(r_ok)
    assert "Claire Vance" in text_ok
    s = ctx.state["session_state"]
    assert s["authenticated"] is True
    assert s["target_agent"] == "dispute_specialist_agent"
    assert s["pending_dispute_init"] is True
    assert s["has_active_case"] is False
    assert len(tools_mock.verify_calls) == 1


def test_authenticate_caller_two_attempt_lockout(load_controllers) -> None:
    """Verify two wrong attempts lock verification and set call_complete='true'."""
    auth_fn, _, ctx, _ = load_controllers()

    r1 = auth_fn(card_last4="4821", passcode="000000")
    text1 = _assert_clean_action(r1)
    assert "did not match" in text1
    assert ctx.state["session_state"]["auth_attempts"] == 1
    assert ctx.state["session_state"].get("call_complete", "") == ""

    r2 = auth_fn(card_last4="4821", passcode="999999")
    text2 = _assert_clean_action(r2)
    assert "still do not match" in text2
    s = ctx.state["session_state"]
    assert s["auth_attempts"] == 2
    assert s["escalation_reason"] == "AUTH_LOCKED"
    assert s["call_complete"] == "true"


def test_assist_card_dispute_happy_path_and_state_matrix(load_controllers) -> None:
    """Verify happy path and empty-arg / stray-confirmation safety across states."""
    auth_fn, dispute_fn, ctx, _ = load_controllers()

    # Unauthenticated call must bounce back to authentication_agent
    unauth = dispute_fn(operation="sync")
    _assert_clean_action(unauth)
    assert ctx.state["session_state"]["target_agent"] == "authentication_agent"

    # Authenticate caller
    auth_fn(card_last4="4821", passcode="195501")

    # Stray confirmation on cold start syncs Step 1 in 1 RPC and reads the transaction menu
    stray = dispute_fn(operation="submit", confirmed="yes")
    stray_text = _assert_clean_action(stray)
    assert "Acme Electronics Ltd" in stray_text
    s = ctx.state["session_state"]
    assert s["expected_step_id"] == "select_transaction"
    assert s["expected_input_kind"] == "OPTION_SELECT"
    assert s["workflow_status"] == "SELECT_TRANSACTION"

    # Stray confirmation when already at OPTION_SELECT returns reprompt_guidance without re-syncing
    stray_after = dispute_fn(operation="submit", confirmed="yes")
    stray_after_text = _assert_clean_action(stray_after)
    assert "could not match that selection" in stray_after_text
    assert ctx.state["session_state"]["expected_step_id"] == "select_transaction"

    # Empty args at select_transaction re-reads the numbered options without resetting state
    repeat1 = dispute_fn()
    repeat1_text = _assert_clean_action(repeat1)
    assert "Acme Electronics Ltd" in repeat1_text
    assert ctx.state["session_state"]["expected_step_id"] == "select_transaction"

    # Select Option 1 (Acme Electronics Ltd £142.50) -> advances to confirm_card_replacement
    step2 = dispute_fn(operation="submit", option_index=1)
    step2_text = _assert_clean_action(step2)
    assert "temporary security hold on card ending 4821" in step2_text
    assert "£142.50" in step2_text
    s = ctx.state["session_state"]
    assert s["expected_step_id"] == "confirm_card_replacement"
    assert s["expected_input_kind"] == "CONFIRMATION"
    assert s["workflow_status"] == "CONFIRM_CARD_REPLACEMENT"
    assert s["card_hold_active"] is True

    # Empty confirmed arg at confirm_card_replacement re-prompts confirmation
    empty_confirm = dispute_fn(operation="submit", confirmed="")
    empty_confirm_text = _assert_clean_action(empty_confirm)
    assert "please confirm with yes or no" in empty_confirm_text
    assert ctx.state["session_state"]["expected_step_id"] == "confirm_card_replacement"

    # Confirm replacement -> advances to awaiting_merchant_representment and closes call
    step3 = dispute_fn(operation="submit", confirmed="yes")
    step3_text = _assert_clean_action(step3)
    assert "replacement card ending 8914" in step3_text
    assert "7-day Visa merchant representment window" in step3_text
    s = ctx.state["session_state"]
    assert s["expected_step_id"] == "awaiting_merchant_representment"
    assert s["expected_input_kind"] == "NONE"
    assert s["workflow_status"] == "AWAITING_MERCHANT_REPRESENTMENT"
    assert s["provisional_credit_gbp"] == "142.50"
    assert s["call_complete"] == "true"


def test_cross_session_resume_vs_mid_call_409_resync(load_controllers) -> None:
    """Verify cross-session resume says Welcome back once, whereas mid-call 409 resync does not."""
    auth_fn, dispute_fn, ctx, _ = load_controllers(
        initial_state={"session_state": {"customer_id": "customer-claire-resume"}}
    )
    auth_fn(card_last4="4821", passcode="195501")
    assert ctx.state["session_state"]["has_active_case"] is True

    # First sync in resumed session includes Welcome back
    resumed = dispute_fn(operation="sync")
    resumed_text = _assert_clean_action(resumed)
    assert "Welcome back, Claire Vance" in resumed_text
    assert ctx.state["session_state"]["has_active_case"] is False
    assert ctx.state["session_state"]["expected_step_id"] == "confirm_card_replacement"

    # Force a stale expected_step_id in the SAME session; 409 resync must NOT say Welcome back
    ctx.state["session_state"]["expected_step_id"] = "select_transaction"
    ctx.state["session_state"]["expected_input_kind"] = "OPTION_SELECT"
    ctx.state["session_state"]["pending_workflow_options"] = [
        "£142.50 at Acme Electronics Ltd on 2 Nov 2026",
        "£29.99 at StreamFlix Digital on 1 Nov 2026",
    ]
    resynced = dispute_fn(operation="submit", option_index=1)
    resynced_text = _assert_clean_action(resynced)
    assert "Welcome back" not in resynced_text
    assert ctx.state["session_state"]["expected_step_id"] == "confirm_card_replacement"


def test_in_progress_timeout_preserves_step_and_retries_same_args(
    load_controllers,
) -> None:
    """Verify status='IN_PROGRESS' keeps expected_step_id and resends identical args next turn."""
    _, dispute_fn, ctx, tools_mock = load_controllers(
        initial_state={
            "session_state": {
                "customer_id": "customer-claire-bristol",
                "authenticated": True,
            }
        }
    )
    dispute_fn(operation="sync")
    assert ctx.state["session_state"]["expected_step_id"] == "select_transaction"

    # Simulate 2.5s timeout returning status="IN_PROGRESS"
    tools_mock.next_error_response = FakeExternalResponse(
        status_code=200,
        payload={
            "status": "IN_PROGRESS",
            "error_code": "",
            "workflow_id": "dispute:customer-claire-bristol:DSP-2026-8841",
            "case_ref": "DSP-2026-8841",
            "workflow_status": "SELECT_TRANSACTION",
            "step_id": "select_transaction",
            "step_seq": 1,
            "input_kind": "OPTION_SELECT",
            "update_id": "conv-default:select_transaction:12345678",
            "context_summary": "Temporal workflow update accepted and still executing.",
            "customer_guidance": (
                "I am still processing that step on your dispute case. Please bear with me "
                "for a moment while I check the status."
            ),
            "reprompt_guidance": "",
            "end_conversation": False,
            "session_vars": {},
            "options": [],
            "selected_transaction": {},
            "provisional_credit_gbp": 0.0,
            "card_hold_active": False,
            "saga_compensated": False,
            "outbound_receipt_sent": False,
            "next_step_estimated_ms": 1000,
        },
    )
    hold_res = dispute_fn(operation="submit", option_index=1)
    hold_text = _assert_clean_action(hold_res)
    assert "still processing" in hold_text
    assert ctx.state["session_state"]["expected_step_id"] == "select_transaction"
    assert ctx.state["session_state"]["pending_in_progress_args"] != {}

    # Next turn with empty args automatically re-sends the saved submit(option_index=1)
    completed_res = dispute_fn()
    completed_text = _assert_clean_action(completed_res)
    assert "Acme Electronics Ltd" in completed_text
    assert ctx.state["session_state"]["expected_step_id"] == "confirm_card_replacement"
    assert ctx.state["session_state"]["pending_in_progress_args"] == {}
    assert tools_mock.step_calls[-1]["operation"] == "submit"
    assert tools_mock.step_calls[-1]["option_index"] == 1


def test_conflicting_option_index_and_option_text_reprompts(
    load_controllers,
) -> None:
    """Verify _resolve_option_index re-prompts when option_index and option_text disagree."""
    _, dispute_fn, ctx, _ = load_controllers(
        initial_state={
            "session_state": {
                "customer_id": "customer-claire-bristol",
                "authenticated": True,
            }
        }
    )
    dispute_fn(operation="sync")
    assert ctx.state["session_state"]["expected_step_id"] == "select_transaction"

    # Option 1 is Acme Electronics Ltd, so passing option_index=1 with StreamFlix must re-prompt
    conflict_res = dispute_fn(
        operation="submit",
        option_index=1,
        option_text="StreamFlix Digital",
    )
    conflict_text = _assert_clean_action(conflict_res)
    assert "could not match that selection" in conflict_text
    assert ctx.state["session_state"]["expected_step_id"] == "select_transaction"

    # Selecting Option 2 with StreamFlix Digital succeeds and reports £29.99
    opt2_res = dispute_fn(
        operation="submit",
        option_index=2,
        option_text="StreamFlix Digital",
    )
    opt2_text = _assert_clean_action(opt2_res)
    assert "£29.99" in opt2_text
    assert "StreamFlix Digital" in opt2_text
    assert ctx.state["session_state"]["expected_step_id"] == "confirm_card_replacement"


def test_platform_string_error_code_409_envelope_resyncs(
    load_controllers,
) -> None:
    """Verify GECX OpenAPI error envelopes with string errorCode ('ABORTED') resync on 409."""
    _, dispute_fn, ctx, tools_mock = load_controllers(
        initial_state={
            "session_state": {
                "customer_id": "customer-claire-resume",
                "authenticated": True,
                "expected_step_id": "select_transaction",
                "expected_input_kind": "OPTION_SELECT",
                "workflow_status": "SELECT_TRANSACTION",
                "pending_workflow_options": [
                    "£142.50 at Acme Electronics Ltd on 2 Nov 2026",
                    "£29.99 at StreamFlix Digital on 1 Nov 2026",
                ],
            }
        }
    )
    tools_mock.next_error_response = FakeExternalResponse(
        status_code=200,
        payload={
            "status": "error",
            "errorCode": "ABORTED",
            "error": (
                "Error fetching from URL - 409 Conflict: Stale step submission: "
                "expected 'confirm_card_replacement', got 'select_transaction'."
            ),
        },
    )
    res = dispute_fn(operation="submit", option_index=1)
    text = _assert_clean_action(res)
    assert "temporary security hold on card ending 4821" in text
    assert ctx.state["session_state"]["expected_step_id"] == "confirm_card_replacement"
    assert ctx.state["session_state"]["workflow_status"] == "CONFIRM_CARD_REPLACEMENT"


def test_cancel_at_both_steps_and_no_option_2_strips_confirmed(
    load_controllers,
) -> None:
    """Verify operation='cancel' at both steps and 'No, option 2' stripping confirmed."""
    # 1. Cancel at select_transaction (no card hold staged yet)
    auth1, dispute1, ctx1, _ = load_controllers(
        initial_state={"session_state": {"customer_id": "customer-claire-bristol"}}
    )
    auth1(card_last4="4821", passcode="195501")
    dispute1(operation="sync")
    cancel_sel = dispute1(operation="cancel")
    cancel_sel_text = _assert_clean_action(cancel_sel)
    assert "cancelled dispute case" in cancel_sel_text
    assert ctx1.state["session_state"]["workflow_status"] == "CANCELLED_COMPENSATED"
    assert ctx1.state["session_state"]["card_hold_active"] is False
    assert ctx1.state["session_state"]["call_complete"] == "true"

    # 2. Saying "No, option 2" at select_transaction strips confirmed="no" from payload
    # and selects Option 2 (StreamFlix Digital), then cancel at confirm_card_replacement
    auth2, dispute2, ctx2, tools2 = load_controllers(
        initial_state={"session_state": {"customer_id": "customer-claire-bristol"}}
    )
    auth2(card_last4="4821", passcode="195501")
    dispute2(operation="sync")
    sel2 = dispute2(
        operation="submit",
        option_index=2,
        option_text="StreamFlix Digital",
        confirmed="no",
    )
    sel2_text = _assert_clean_action(sel2)
    assert "StreamFlix Digital" in sel2_text
    assert "£29.99" in sel2_text
    assert tools2.step_calls[-1]["operation"] == "submit"
    assert tools2.step_calls[-1]["option_index"] == 2
    assert tools2.step_calls[-1]["confirmed"] == ""
    assert ctx2.state["session_state"]["expected_step_id"] == "confirm_card_replacement"
    assert ctx2.state["session_state"]["card_hold_active"] is True

    cancel_conf = dispute2(operation="cancel")
    cancel_conf_text = _assert_clean_action(cancel_conf)
    assert "cancelled dispute case" in cancel_conf_text
    assert ctx2.state["session_state"]["workflow_status"] == "CANCELLED_COMPENSATED"
    assert ctx2.state["session_state"]["card_hold_active"] is False
    assert ctx2.state["session_state"]["call_complete"] == "true"


def test_resume_with_mismatched_submit_and_temporal_session_vars_overwrite(
    load_controllers,
) -> None:
    """Verify uninitialized resume with mismatched submit preserves Welcome back in 1 RPC
    and Temporal session_vars can freely set or overwrite keys inside session_state."""
    auth_fn, dispute_fn, ctx, tools_mock = load_controllers(
        initial_state={"session_state": {"customer_id": "customer-claire-resume"}}
    )
    auth_fn(card_last4="4821", passcode="195501")
    assert ctx.state["session_state"]["has_active_case"] is True

    # Passing option_index=1 on an uninitialized resumed session at CONFIRMATION
    # syncs once (1 RPC) and returns the full "Welcome back, Claire Vance..." guidance
    res = dispute_fn(operation="submit", option_index=1)
    text = _assert_clean_action(res)
    assert len(tools_mock.step_calls) == 1
    assert tools_mock.step_calls[0]["operation"] == "sync"
    assert tools_mock.step_calls[0]["is_session_resume"] is True
    assert "Welcome back, Claire Vance" in text
    assert ctx.state["session_state"]["expected_step_id"] == "confirm_card_replacement"
    assert ctx.state["session_state"]["expected_input_kind"] == "CONFIRMATION"

    # Inject a fake response containing custom and overwritten keys in session_vars
    tools_mock.next_error_response = FakeExternalResponse(
        status_code=200,
        payload={
            "status": "SUCCESS",
            "error_code": "",
            "workflow_id": "dispute:customer-claire-resume:DSP-2026-8841",
            "case_ref": "DSP-2026-8841",
            "workflow_status": "CONFIRM_CARD_REPLACEMENT",
            "step_id": "confirm_card_replacement",
            "step_seq": 2,
            "input_kind": "CONFIRMATION",
            "update_id": "conv-default:confirm_card_replacement:abcd1234",
            "context_summary": "Summary.",
            "customer_guidance": "Guidance.",
            "reprompt_guidance": "Reprompt.",
            "end_conversation": False,
            "session_vars": {
                "workflow_status": "CONFIRM_CARD_REPLACEMENT",
                "card_hold_active": True,
                "provisional_credit_gbp": "142.50",
                "case_ref": "DSP-2026-9999",
            },
            "options": [],
            "selected_transaction": {},
            "provisional_credit_gbp": 142.5,
            "card_hold_active": True,
            "saga_compensated": False,
            "outbound_receipt_sent": False,
            "next_step_estimated_ms": 400,
        },
    )
    dispute_fn(operation="sync")
    # Temporal session_vars can set or overwrite any key inside session_state
    assert ctx.state["session_state"]["provisional_credit_gbp"] == "142.50"
    assert ctx.state["session_state"]["case_ref"] == "DSP-2026-9999"

    # Verify tool_fake_config rejects unknown keys like legacy 'action' with 422
    legacy_res = tools_mock.fake_mod.fake_tool_call(
        type("T", (), {"name": "temporal_workflow_api_advance_dispute"})(),
        {"customer_id": "customer-claire-bristol", "action": "start_or_status"},
        None,
    )
    assert legacy_res["errorCode"] == 422

    # B1 regression: in tool_fake_config, committing confirmed="yes" on customer-claire-resume
    # and then syncing in the same session preserves awaiting_merchant_representment and £142.50
    auth_b1, disp_b1, ctx_b1, _ = load_controllers(
        initial_state={"session_state": {"customer_id": "customer-claire-resume"}}
    )
    auth_b1(card_last4="4821", passcode="195501")
    disp_b1(operation="sync")
    assert ctx_b1.state["session_state"]["expected_step_id"] == "confirm_card_replacement"
    wf_id_before = ctx_b1.state["session_state"]["workflow_id"]
    disp_b1(operation="submit", confirmed="yes")
    assert (
        ctx_b1.state["session_state"]["expected_step_id"]
        == "awaiting_merchant_representment"
    )
    assert ctx_b1.state["session_state"]["provisional_credit_gbp"] == "142.50"
    disp_b1(operation="sync")
    assert ctx_b1.state["session_state"]["workflow_id"] == wf_id_before
    assert (
        ctx_b1.state["session_state"]["expected_step_id"]
        == "awaiting_merchant_representment"
    )
    assert ctx_b1.state["session_state"]["provisional_credit_gbp"] == "142.50"
