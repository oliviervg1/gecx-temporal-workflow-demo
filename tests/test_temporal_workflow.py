"""Unit, concurrency, time-skipping, and replay tests for DisputeResolutionWorkflow."""

import asyncio
import contextlib
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from temporal_service.activities import SIDE_EFFECT_LEDGER
from temporal_service.app import app, runtime_manager
from temporal_service.models import AdvanceDisputeRequest, compute_step_update_id
from temporal_service.workflows import DisputeResolutionWorkflow

CID = "customer-claire-bristol"


@pytest.fixture(autouse=True)
def clear_side_effect_ledger() -> None:
    """Resets the in-memory side-effect ledger before each test."""
    SIDE_EFFECT_LEDGER.clear()


@pytest.mark.asyncio
async def test_happy_path_update_with_start_and_cross_session_resume() -> None:
    """Verifies Update-With-Start, step progression, merchant signal, and Session-2 resume."""
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    try:
        await runtime_manager.start(existing_client=env.client)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Pre-auth verify check (no credentials yet -> has_active_case=False)
            pre = await client.post(
                "/v1/dispute-workflow/verify",
                json={"customer_id": CID},
            )
            assert pre.status_code == 200
            assert pre.json()["status"] == "INVALID_CREDENTIALS"
            assert pre.json()["has_active_case"] is False
            assert pre.json()["active_case_ref"] == ""

            # 2. Authenticate caller with valid card_last4 and passcode
            ver = await client.post(
                "/v1/dispute-workflow/verify",
                json={
                    "customer_id": CID,
                    "card_last4": "4821",
                    "passcode": "195501",
                },
            )
            assert ver.status_code == 200
            assert ver.json()["status"] == "VERIFIED"
            assert ver.json()["customer_name"] == "Claire Vance"
            assert ver.json()["has_active_case"] is False

            # 3. Start workflow via Update-With-Start (operation="sync")
            s1 = await client.post(
                "/v1/dispute-workflow/step",
                json={
                    "customer_id": CID,
                    "conversation_ref": "sess-1",
                    "operation": "sync",
                },
            )
            assert s1.status_code == 200
            b1 = s1.json()
            assert b1["workflow_status"] == "SELECT_TRANSACTION"
            assert b1["step_id"] == "select_transaction"
            assert b1["input_kind"] == "OPTION_SELECT"
            assert b1["end_conversation"] is False
            assert "Please choose 1 for" in b1["reprompt_guidance"]
            assert b1["session_vars"]["workflow_status"] == "SELECT_TRANSACTION"
            assert b1["step_seq"] == 1
            assert len(b1["options"]) == 2

            # 4. Select option 1 (£142.50 at Acme Electronics Ltd)
            s2 = await client.post(
                "/v1/dispute-workflow/step",
                json={
                    "customer_id": CID,
                    "conversation_ref": "sess-1",
                    "expected_step_id": "select_transaction",
                    "operation": "submit",
                    "option_index": 1,
                },
            )
            assert s2.status_code == 200
            b2 = s2.json()
            assert b2["workflow_status"] == "CONFIRM_CARD_REPLACEMENT"
            assert b2["step_id"] == "confirm_card_replacement"
            assert b2["input_kind"] == "CONFIRMATION"
            assert b2["end_conversation"] is False
            assert b2["card_hold_active"] is True
            assert b2["session_vars"]["card_hold_active"] is True
            assert SIDE_EFFECT_LEDGER[CID]["card_hold_active"] is True

            # 4b. Session 2 resume while workflow is still RUNNING at confirm_card_replacement
            ver_mid = await client.post(
                "/v1/dispute-workflow/verify",
                json={"customer_id": CID, "card_last4": "4821", "passcode": "195501"},
            )
            assert ver_mid.json()["has_active_case"] is True
            assert ver_mid.json()["active_case_ref"] == "DSP-2026-8841"

            # Wrong credentials must NOT leak has_active_case or active_case_ref
            ver_bad = await client.post(
                "/v1/dispute-workflow/verify",
                json={"customer_id": CID, "card_last4": "4821", "passcode": "000000"},
            )
            assert ver_bad.json()["status"] == "INVALID_CREDENTIALS"
            assert ver_bad.json()["has_active_case"] is False
            assert ver_bad.json()["active_case_ref"] == ""

            resume_mid = await client.post(
                "/v1/dispute-workflow/step",
                json={
                    "customer_id": CID,
                    "conversation_ref": "sess-mid",
                    "operation": "sync",
                    "is_session_resume": True,
                },
            )
            assert resume_mid.status_code == 200
            assert resume_mid.json()["step_id"] == "confirm_card_replacement"
            assert resume_mid.json()["customer_guidance"].startswith(
                "Welcome back, Claire Vance. Resuming active dispute case DSP-2026-8841."
            )

            # 5. Confirm card block & replacement -> issues £142.50 provisional credit
            s3 = await client.post(
                "/v1/dispute-workflow/step",
                json={
                    "customer_id": CID,
                    "conversation_ref": "sess-1",
                    "expected_step_id": "confirm_card_replacement",
                    "operation": "submit",
                    "confirmed": "yes",
                },
            )
            assert s3.status_code == 200
            b3 = s3.json()
            assert b3["workflow_status"] == "AWAITING_MERCHANT_REPRESENTMENT"
            assert b3["input_kind"] == "NONE"
            assert b3["end_conversation"] is True
            assert b3["provisional_credit_gbp"] == 142.50
            assert b3["session_vars"]["provisional_credit_gbp"] == "142.50"
            assert b3["outbound_receipt_sent"] is True
            assert b3["card_hold_active"] is False

            # 6. Simulate async merchant representment acceptance via Signal
            sig = await client.post(
                "/v1/dispute-workflow/signal",
                json={
                    "customer_id": CID,
                    "signal_name": "simulate_merchant_outcome",
                    "payload": "MERCHANT_ACCEPTED_LIABILITY",
                },
            )
            assert sig.status_code == 200

            # 7. Customer calls back in Session 2 ("sess-2") -> returns settled state
            s4 = await client.post(
                "/v1/dispute-workflow/step",
                json={
                    "customer_id": CID,
                    "conversation_ref": "sess-2",
                    "operation": "sync",
                    "is_session_resume": True,
                },
            )
            assert s4.status_code == 200
            b4 = s4.json()
            assert b4["workflow_status"] == "RESOLVED_CREDITED"
            assert b4["step_id"] == "resolved_credited"
            assert b4["end_conversation"] is True
            assert b4["provisional_credit_gbp"] == 142.50
            assert not b4["customer_guidance"].startswith("Welcome back,")

            # 8. Mutating a completed case returns CASE_CLOSED rather than reopening silently
            closed_mut = await client.post(
                "/v1/dispute-workflow/step",
                json={
                    "customer_id": CID,
                    "conversation_ref": "sess-2",
                    "expected_step_id": "select_transaction",
                    "operation": "submit",
                    "option_index": 1,
                },
            )
            assert closed_mut.status_code == 200
            assert closed_mut.json()["status"] == "ERROR"
            assert closed_mut.json()["error_code"] == "CASE_CLOSED"
            assert closed_mut.json()["end_conversation"] is True

            # 9. Verify deterministic replay using Temporal's Replayer over captured history
            handle = env.client.get_workflow_handle("dispute:customer-claire-bristol:DSP-2026-8841")
            history = await handle.fetch_history()
            replayer = Replayer(
                workflows=[DisputeResolutionWorkflow],
                data_converter=pydantic_data_converter,
            )
            await replayer.replay_workflow(history)
    finally:
        await runtime_manager.stop()
        await env.shutdown()


@pytest.mark.asyncio
async def test_validator_rejects_stale_expected_step_id_and_invalid_option() -> None:
    """Verifies @submit_step.validator returns 409 for stale steps and 422 for invalid options."""
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    try:
        await runtime_manager.start(existing_client=env.client)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                "/v1/dispute-workflow/step",
                json={
                    "customer_id": CID,
                    "conversation_ref": "sess-val",
                    "operation": "sync",
                },
            )

            # Submitting confirm_card_replacement while at select_transaction fails 409
            stale = await client.post(
                "/v1/dispute-workflow/step",
                json={
                    "customer_id": CID,
                    "conversation_ref": "sess-val",
                    "expected_step_id": "confirm_card_replacement",
                    "operation": "submit",
                    "confirmed": "yes",
                },
            )
            assert stale.status_code == 409
            assert "Stale step submission" in stale.json()["detail"]

            # Submitting option_index=99 fails validator with 422 (InvalidInput)
            bad_opt = await client.post(
                "/v1/dispute-workflow/step",
                json={
                    "customer_id": CID,
                    "conversation_ref": "sess-val",
                    "expected_step_id": "select_transaction",
                    "operation": "submit",
                    "option_index": 99,
                },
            )
            assert bad_opt.status_code == 422
            assert "Invalid option_index" in bad_opt.json()["detail"]

            # Legacy 'action' key is rejected by ConfigDict(extra='forbid') with 422
            legacy_action = await client.post(
                "/v1/dispute-workflow/step",
                json={
                    "customer_id": CID,
                    "conversation_ref": "sess-val",
                    "action": "start_or_status",
                },
            )
            assert legacy_action.status_code == 422

            # Saying "No, option 2" (operation="submit", option_index=2, confirmed="no")
            # at OPTION_SELECT must select option 2 rather than cancelling!
            sel2 = await client.post(
                "/v1/dispute-workflow/step",
                json={
                    "customer_id": CID,
                    "conversation_ref": "sess-val",
                    "expected_step_id": "select_transaction",
                    "operation": "submit",
                    "option_index": 2,
                    "confirmed": "no",
                },
            )
            assert sel2.status_code == 200
            assert sel2.json()["workflow_status"] == "CONFIRM_CARD_REPLACEMENT"
            assert sel2.json()["selected_transaction"]["merchant_name"] == "StreamFlix Digital"
            assert sel2.json()["options"] == []

            # Submitting CONFIRMATION step with empty confirmed="" fails validator with 422
            bad_conf = await client.post(
                "/v1/dispute-workflow/step",
                json={
                    "customer_id": CID,
                    "conversation_ref": "sess-val",
                    "expected_step_id": "confirm_card_replacement",
                    "operation": "submit",
                    "confirmed": "",
                },
            )
            assert bad_conf.status_code == 422
            assert "Invalid confirmed" in bad_conf.json()["detail"]

            # Explicit operation="cancel" at confirm_card_replacement releases card hold
            cancel_conf = await client.post(
                "/v1/dispute-workflow/step",
                json={
                    "customer_id": CID,
                    "conversation_ref": "sess-val",
                    "expected_step_id": "confirm_card_replacement",
                    "operation": "cancel",
                },
            )
            assert cancel_conf.status_code == 200
            assert cancel_conf.json()["workflow_status"] == "CANCELLED_COMPENSATED"
            assert cancel_conf.json()["saga_compensated"] is True
            assert cancel_conf.json()["card_hold_active"] is False
            assert cancel_conf.json()["end_conversation"] is True

            # Reset, start with is_session_resume=True on cold start -> no "Welcome back" prefix
            await client.post(f"/v1/dispute-workflow/{CID}/reset")
            cold_sync = await client.post(
                "/v1/dispute-workflow/step",
                json={
                    "customer_id": CID,
                    "conversation_ref": "sess-cold",
                    "operation": "sync",
                    "is_session_resume": True,
                },
            )
            assert cold_sync.status_code == 200
            assert not cold_sync.json()["customer_guidance"].startswith("Welcome back,")

            # Explicit operation="cancel" at select_transaction closes cleanly with no hold
            cancel_sel = await client.post(
                "/v1/dispute-workflow/step",
                json={
                    "customer_id": CID,
                    "conversation_ref": "sess-cold",
                    "expected_step_id": "select_transaction",
                    "operation": "cancel",
                },
            )
            assert cancel_sel.status_code == 200
            assert cancel_sel.json()["workflow_status"] == "CANCELLED_COMPENSATED"
            assert cancel_sel.json()["saga_compensated"] is False
            assert cancel_sel.json()["end_conversation"] is True
    finally:
        await runtime_manager.stop()
        await env.shutdown()


def test_compute_step_update_id_distinguishes_input_text() -> None:
    """Verifies compute_step_update_id hashes input_text distinctly alongside other args."""
    r1 = AdvanceDisputeRequest(
        conversation_ref="c1",
        expected_step_id="free_text_step",
        operation="submit",
        input_text="first reason",
    )
    r2 = AdvanceDisputeRequest(
        conversation_ref="c1",
        expected_step_id="free_text_step",
        operation="submit",
        input_text="second reason",
    )
    assert compute_step_update_id(r1) != compute_step_update_id(r2)


@pytest.mark.asyncio
async def test_workflow_edge_case_regressions() -> None:
    """Verifies multi-turn sync, resume banner, and step sequence edge cases pass cleanly."""
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    try:
        await runtime_manager.start(existing_client=env.client)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Probe M3: Repeated sync in same session after select returns fresh step
            await client.post(
                "/v1/dispute-workflow/step",
                json={"customer_id": CID, "conversation_ref": "s1", "operation": "sync"},
            )
            await client.post(
                "/v1/dispute-workflow/step",
                json={
                    "customer_id": CID,
                    "conversation_ref": "s1",
                    "expected_step_id": "select_transaction",
                    "operation": "submit",
                    "option_index": 1,
                },
            )
            r_status = await client.post(
                "/v1/dispute-workflow/step",
                json={"customer_id": CID, "conversation_ref": "s1", "operation": "sync"},
            )
            assert r_status.json()["step_id"] == "confirm_card_replacement"
            assert r_status.json()["step_seq"] == 2

            # Probe M1: Retrying submit without expected_step_id at confirm is 409
            r_retry_select = await client.post(
                "/v1/dispute-workflow/step",
                json={
                    "customer_id": CID,
                    "conversation_ref": "s1b",
                    "operation": "submit",
                    "option_index": 1,
                },
            )
            assert r_retry_select.status_code == 409
            assert r_retry_select.json().get("workflow_status") != "AWAITING_MERCHANT_REPRESENTMENT"

            # Probe M4: Reset after completion allows a clean fresh start
            await client.post(
                "/v1/dispute-workflow/step",
                json={
                    "customer_id": CID,
                    "conversation_ref": "s1",
                    "expected_step_id": "confirm_card_replacement",
                    "operation": "submit",
                    "confirmed": "no",
                },
            )
            await client.post(f"/v1/dispute-workflow/{CID}/reset")
            r_after_reset = await client.post(
                "/v1/dispute-workflow/step",
                json={"customer_id": CID, "conversation_ref": "fresh", "operation": "sync"},
            )
            assert r_after_reset.status_code == 200
            assert r_after_reset.json()["workflow_status"] == "SELECT_TRANSACTION"
            assert r_after_reset.json()["step_seq"] == 1

            # Probe M2: Concurrent selects (TOCTOU race) — one succeeds, the second is rejected 409
            r1, r2 = await asyncio.gather(
                client.post(
                    "/v1/dispute-workflow/step",
                    json={
                        "customer_id": CID,
                        "conversation_ref": "a",
                        "expected_step_id": "select_transaction",
                        "operation": "submit",
                        "option_index": 1,
                    },
                ),
                client.post(
                    "/v1/dispute-workflow/step",
                    json={
                        "customer_id": CID,
                        "conversation_ref": "b",
                        "expected_step_id": "select_transaction",
                        "operation": "submit",
                        "option_index": 2,
                    },
                ),
            )
            statuses = sorted([r1.status_code, r2.status_code])
            assert statuses == [200, 409]
            winner = r1 if r1.status_code == 200 else r2
            assert winner.json()["workflow_status"] == "CONFIRM_CARD_REPLACEMENT"
    finally:
        await runtime_manager.stop()
        await env.shutdown()


@pytest.mark.asyncio
async def test_abandonment_timeout_and_signal_trigger_saga_compensation() -> None:
    """Verifies 15-minute hang-up timeout and caller_abandoned signal release the card hold."""
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    try:
        await runtime_manager.start(existing_client=env.client)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                "/v1/dispute-workflow/step",
                json={
                    "customer_id": CID,
                    "conversation_ref": "sess-ab",
                    "operation": "sync",
                },
            )
            s2 = await client.post(
                "/v1/dispute-workflow/step",
                json={
                    "customer_id": CID,
                    "conversation_ref": "sess-ab",
                    "expected_step_id": "select_transaction",
                    "operation": "submit",
                    "option_index": 1,
                },
            )
            assert s2.json()["card_hold_active"] is True

            # Advance virtual workflow clock by 16 minutes to trip HUMAN_ABANDONMENT_TIMEOUT (15m)
            await env.sleep(timedelta(minutes=16))

            inspect = await client.get(f"/v1/dispute-workflow/{CID}/inspect")
            assert inspect.status_code == 200
            data = inspect.json()
            assert data["live_state"]["workflow_status"] == "ABANDONED_COMPENSATED"
            assert data["live_state"]["card_hold_active"] is False
            assert data["live_state"]["saga_compensated"] is True
            assert SIDE_EFFECT_LEDGER[CID]["card_hold_active"] is False
            assert SIDE_EFFECT_LEDGER[CID]["saga_compensated"] is True

            # Reset and verify explicit caller_abandoned signal also compensates immediately
            await client.post(f"/v1/dispute-workflow/{CID}/reset")
            await client.post(
                "/v1/dispute-workflow/step",
                json={"customer_id": CID, "conversation_ref": "s-sig", "operation": "sync"},
            )
            await client.post(
                "/v1/dispute-workflow/step",
                json={
                    "customer_id": CID,
                    "conversation_ref": "s-sig",
                    "expected_step_id": "select_transaction",
                    "operation": "submit",
                    "option_index": 1,
                },
            )
            await client.post(
                "/v1/dispute-workflow/signal",
                json={"customer_id": CID, "signal_name": "caller_abandoned", "payload": "HANGUP"},
            )
            await env.sleep(timedelta(seconds=1))
            inspect2 = await client.get(f"/v1/dispute-workflow/{CID}/inspect")
            assert inspect2.json()["live_state"]["workflow_status"] == "ABANDONED_COMPENSATED"
            assert SIDE_EFFECT_LEDGER[CID]["card_hold_active"] is False
    finally:
        await runtime_manager.stop()
        await env.shutdown()


@pytest.mark.asyncio
async def test_seven_day_merchant_window_timer_and_workflow_cancellation() -> None:
    """Verifies 7-day merchant window auto-settlement and workflow cancellation compensation."""
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    try:
        await runtime_manager.start(existing_client=env.client)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                "/v1/dispute-workflow/step",
                json={"customer_id": CID, "conversation_ref": "s-7d", "operation": "sync"},
            )
            await client.post(
                "/v1/dispute-workflow/step",
                json={
                    "customer_id": CID,
                    "conversation_ref": "s-7d",
                    "expected_step_id": "select_transaction",
                    "operation": "submit",
                    "option_index": 1,
                },
            )
            # Duplicate retry with identical conversation_ref + expected_step_id + args deduplicates
            dup = await client.post(
                "/v1/dispute-workflow/step",
                json={
                    "customer_id": CID,
                    "conversation_ref": "s-7d",
                    "expected_step_id": "select_transaction",
                    "operation": "submit",
                    "option_index": 1,
                },
            )
            assert dup.status_code == 200
            assert dup.json()["workflow_status"] == "CONFIRM_CARD_REPLACEMENT"

            await client.post(
                "/v1/dispute-workflow/step",
                json={
                    "customer_id": CID,
                    "conversation_ref": "s-7d",
                    "expected_step_id": "confirm_card_replacement",
                    "operation": "submit",
                    "confirmed": "yes",
                },
            )
            # At awaiting_merchant_representment (input_kind="NONE"), submit and cancel are 409
            non_interactive_sub = await client.post(
                "/v1/dispute-workflow/step",
                json={
                    "customer_id": CID,
                    "conversation_ref": "s-7d",
                    "expected_step_id": "awaiting_merchant_representment",
                    "operation": "submit",
                },
            )
            assert non_interactive_sub.status_code == 409
            non_interactive_can = await client.post(
                "/v1/dispute-workflow/step",
                json={
                    "customer_id": CID,
                    "conversation_ref": "s-7d",
                    "expected_step_id": "awaiting_merchant_representment",
                    "operation": "cancel",
                },
            )
            assert non_interactive_can.status_code == 409

            # Advance virtual clock by 8 days to elapse the 7-day merchant representment window
            await env.sleep(timedelta(days=8))
            settled = await client.post(
                "/v1/dispute-workflow/step",
                json={"customer_id": CID, "conversation_ref": "s-8d", "operation": "sync"},
            )
            assert settled.json()["workflow_status"] == "RESOLVED_CREDITED"
            assert "REPRESENTMENT_WINDOW_ELAPSED_FINAL" in settled.json()["context_summary"]

            # Reset, stage a card hold, and cancel the workflow via handle.cancel() -> compensates
            reset_resp = await client.post(f"/v1/dispute-workflow/{CID}/reset")
            wf_id = reset_resp.json()["next_workflow_id"]
            await client.post(
                "/v1/dispute-workflow/step",
                json={"customer_id": CID, "conversation_ref": "s-can", "operation": "sync"},
            )
            await client.post(
                "/v1/dispute-workflow/step",
                json={
                    "customer_id": CID,
                    "conversation_ref": "s-can",
                    "expected_step_id": "select_transaction",
                    "operation": "submit",
                    "option_index": 2,
                },
            )
            assert SIDE_EFFECT_LEDGER[CID]["card_hold_active"] is True
            handle = env.client.get_workflow_handle(wf_id)
            await handle.cancel()
            with contextlib.suppress(Exception):
                await handle.result()
            assert SIDE_EFFECT_LEDGER[CID]["card_hold_active"] is False
            assert SIDE_EFFECT_LEDGER[CID]["saga_compensated"] is True

            # B1 regression: for resume-fixture customer (customer-claire-resume),
            # verify -> sync -> submit(confirmed="yes") -> subsequent sync in the same
            # session must preserve the committed workflow_id, step_id, and £142.50 credit.
            ver_res = await client.post(
                "/v1/dispute-workflow/verify",
                json={
                    "customer_id": "customer-claire-resume",
                    "card_last4": "4821",
                    "passcode": "195501",
                },
            )
            assert ver_res.status_code == 200
            assert ver_res.json()["has_active_case"] is True

            sync1 = await client.post(
                "/v1/dispute-workflow/step",
                json={
                    "customer_id": "customer-claire-resume",
                    "conversation_ref": "s-resume-b1",
                    "operation": "sync",
                    "is_session_resume": True,
                },
            )
            assert sync1.status_code == 200
            assert sync1.json()["step_id"] == "confirm_card_replacement"
            committed_wf_id = sync1.json()["workflow_id"]

            commit_res = await client.post(
                "/v1/dispute-workflow/step",
                json={
                    "customer_id": "customer-claire-resume",
                    "conversation_ref": "s-resume-b1",
                    "expected_step_id": "confirm_card_replacement",
                    "operation": "submit",
                    "confirmed": "yes",
                },
            )
            assert commit_res.status_code == 200
            assert commit_res.json()["step_id"] == "awaiting_merchant_representment"
            assert commit_res.json()["provisional_credit_gbp"] == 142.50

            sync2 = await client.post(
                "/v1/dispute-workflow/step",
                json={
                    "customer_id": "customer-claire-resume",
                    "conversation_ref": "s-resume-b1",
                    "expected_step_id": "awaiting_merchant_representment",
                    "operation": "sync",
                },
            )
            assert sync2.status_code == 200
            assert sync2.json()["workflow_id"] == committed_wf_id
            assert sync2.json()["step_id"] == "awaiting_merchant_representment"
            assert sync2.json()["provisional_credit_gbp"] == 142.50
    finally:
        await runtime_manager.stop()
        await env.shutdown()
