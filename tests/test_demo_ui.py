"""End-to-end HTTP tests for the Split-Screen Demo UI server (demo_ui/server.py).

Verifies that:
  1. `boot_embedded_temporal` keeps the `WorkflowEnvironment` alive across `gc.collect()` (U1).
  2. The Demo UI HTTP server bridges conversational turns, cross-session resume while
     `RUNNING` in the 7-day window, Temporal signals (7-day fast-forward and caller-abandoned
     saga rollback), and live workflow inspection.
  3. Selecting Option 2 ("Option 2 please, the £29.99 StreamFlix payment on 1 November.")
     routes to Option 2 (StreamFlix Digital, £29.99) and returns dynamic £29.99 speech (U2/U3).
"""

import asyncio
import gc
import json
import threading
import urllib.request
from typing import Any

from demo_ui.server import (
    AdapterBridge,
    ThreadedHTTPServer,
    boot_embedded_temporal,
    make_handler,
)
from temporal_service.app import runtime_manager


def _http_json(
    base_url: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(req, timeout=15.0) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_demo_ui_end_to_end_journey_option2_and_saga_rollback() -> None:
    """Exercise boot_embedded_temporal (post-GC), happy path, Option 2, and saga rollback."""
    asgi_loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=asgi_loop.run_forever, daemon=True)
    loop_thread.start()

    env, asgi_client = boot_embedded_temporal(asgi_loop)
    # Force garbage collection to verify U1 (env is returned and strongly referenced).
    gc.collect()

    bridge = AdapterBridge(
        adapter_url="http://embedded-temporal",
        token_cache=None,
        asgi_loop=asgi_loop,
        asgi_client=asgi_client,
    )
    handler_cls = make_handler(
        bridge=bridge,
        token_cache=None,
        project_id="test-gcp-project",
        cxas_location="eu",
        app_name="",
    )
    http_server = ThreadedHTTPServer(("127.0.0.1", 0), handler_cls)
    port = http_server.server_address[1]
    base_url = f"http://127.0.0.1:{port}"
    server_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    server_thread.start()

    try:
        # 1. Verify HTML index, health check, and config endpoint.
        with urllib.request.urlopen(f"{base_url}/", timeout=5.0) as html_resp:
            html_body = html_resp.read().decode("utf-8")
            assert html_resp.status == 200
            assert "Albion Bank" in html_body
            assert "DisputeResolutionWorkflow" in html_body
            assert "prompts-stage-label" in html_body
            assert "chip-reset-start" in html_body

        status, health = _http_json(base_url, "GET", "/health")
        assert status == 200
        assert health["status"] == "ok"

        status, cfg = _http_json(base_url, "GET", "/api/config")
        assert status == 200
        assert cfg["default_customer_id"] == "customer-claire-bristol"

        # Reset before starting.
        status, _ = _http_json(
            base_url,
            "POST",
            "/api/workflow/reset",
            {"customer_id": "customer-claire-bristol"},
        )
        assert status == 200

        # 2. Turn 1: ID&V + auto-transfer to dispute_specialist_agent + operation="sync".
        status, t1 = _http_json(
            base_url,
            "POST",
            "/api/session/turn",
            {
                "customer_id": "customer-claire-bristol",
                "session_id": "session-call-1",
                "text": "Card ending 4821 and passcode 195501",
                "use_local_runtime": True,
            },
        )
        assert status == 200
        assert t1["active_agent"] == "dispute_specialist_agent"
        assert t1["agent_transitions"] == [
            "authentication_agent",
            "dispute_specialist_agent",
        ]
        assert "Acme Electronics Ltd" in t1["agent_text"]
        assert t1["workflow_inspect"]["exists"] is True
        assert t1["workflow_inspect"]["execution_status"] == "RUNNING"
        assert (
            t1["workflow_inspect"]["live_state"]["workflow_status"]
            == "SELECT_TRANSACTION"
        )

        # 3. Turn 2: Select Option 1 (£142.50 at Acme Electronics Ltd).
        status, t2 = _http_json(
            base_url,
            "POST",
            "/api/session/turn",
            {
                "customer_id": "customer-claire-bristol",
                "session_id": "session-call-1",
                "text": "Option 1 please, the £142.50 charge at Acme Electronics",
                "use_local_runtime": True,
            },
        )
        assert status == 200
        wf_state_t2 = t2["workflow_inspect"]["live_state"]
        assert wf_state_t2["workflow_status"] == "CONFIRM_CARD_REPLACEMENT"
        assert wf_state_t2["card_hold_active"] is True
        assert "£142.50" in t2["agent_text"]

        # 4. Turn 3: Confirm provisional credit + replacement card 8914.
        status, t3 = _http_json(
            base_url,
            "POST",
            "/api/session/turn",
            {
                "customer_id": "customer-claire-bristol",
                "session_id": "session-call-1",
                "text": "Yes, please go ahead and issue the provisional credit",
                "use_local_runtime": True,
            },
        )
        assert status == 200
        wf_state_t3 = t3["workflow_inspect"]["live_state"]
        assert wf_state_t3["workflow_status"] == "AWAITING_MERCHANT_REPRESENTMENT"
        assert wf_state_t3["provisional_credit_gbp"] == 142.50
        assert "8914" in t3["agent_text"]
        assert t3["session_ended"] is True

        # 4b. Verify automatic session rotation if a turn is sent on an already-ended session_id.
        status, t3_followup = _http_json(
            base_url,
            "POST",
            "/api/session/turn",
            {
                "customer_id": "customer-claire-bristol",
                "session_id": "session-call-1",
                "text": "Card ending 4821, passcode 195501",
                "use_local_runtime": True,
            },
        )
        assert status == 200
        assert t3_followup["session_rotated"] is True
        assert t3_followup["session_id"] == "session-call-1-r1"
        assert "Welcome back, Claire Vance" in t3_followup["agent_text"]
        assert t3_followup["session_ended"] is True

        # 5. Start a brand-new call session ("session-call-2") while RUNNING in 7-day window.
        status, t4 = _http_json(
            base_url,
            "POST",
            "/api/session/turn",
            {
                "customer_id": "customer-claire-bristol",
                "session_id": "session-call-2",
                "text": "Card ending 4821, passcode 195501",
                "use_local_runtime": True,
            },
        )
        assert status == 200
        assert "Welcome back, Claire Vance" in t4["agent_text"]
        assert "7-day Visa merchant representment window" in t4["agent_text"]

        # 6. Fast-forward the 7-day merchant representment window via Signal.
        status, sig_res = _http_json(
            base_url,
            "POST",
            "/api/workflow/signal",
            {
                "customer_id": "customer-claire-bristol",
                "signal_name": "simulate_merchant_outcome",
                "payload": "MERCHANT_ACCEPTED_LIABILITY",
            },
        )
        assert status == 200
        assert (
            sig_res["workflow_inspect"]["live_state"]["workflow_status"]
            == "RESOLVED_CREDITED"
        )

        # 7. Verify U2 & U3: Reset, start a new dispute, and select Option 2 ("on 1 November").
        _http_json(
            base_url,
            "POST",
            "/api/workflow/reset",
            {"customer_id": "customer-claire-bristol"},
        )
        _http_json(
            base_url,
            "POST",
            "/api/session/turn",
            {
                "customer_id": "customer-claire-bristol",
                "session_id": "session-call-opt2",
                "text": "My card ends 4821 and passcode is 195501.",
                "use_local_runtime": True,
            },
        )
        status, opt2_turn = _http_json(
            base_url,
            "POST",
            "/api/session/turn",
            {
                "customer_id": "customer-claire-bristol",
                "session_id": "session-call-opt2",
                "text": "Option 2 please, the £29.99 StreamFlix payment on 1 November.",
                "use_local_runtime": True,
            },
        )
        assert status == 200
        assert opt2_turn["tool_calls"][-1]["args"]["option_index"] == 2
        assert (
            opt2_turn["tool_calls"][-1]["args"]["option_text"]
            == "StreamFlix Digital"
        )
        assert "£29.99" in opt2_turn["agent_text"]
        assert "StreamFlix Digital" in opt2_turn["agent_text"]
        assert "£142.50" not in opt2_turn["agent_text"]
        assert (
            opt2_turn["workflow_inspect"]["live_state"]["selected_transaction"][
                "merchant_name"
            ]
            == "StreamFlix Digital"
        )

        # 8. Trigger saga rollback on the active Option 2 card hold via caller_abandoned signal.
        assert opt2_turn["workflow_inspect"]["live_state"]["card_hold_active"] is True
        status, abandon_res = _http_json(
            base_url,
            "POST",
            "/api/workflow/signal",
            {
                "customer_id": "customer-claire-bristol",
                "signal_name": "caller_abandoned",
                "payload": "CALLER_HUNG_UP",
            },
        )
        assert status == 200
        abandoned_state = abandon_res["workflow_inspect"]["live_state"]
        assert abandoned_state["workflow_status"] == "ABANDONED_COMPENSATED"
        assert abandoned_state["card_hold_active"] is False
        assert abandoned_state["saga_compensated"] is True
    finally:
        http_server.shutdown()
        http_server.server_close()

        async def _stop_temporal() -> None:
            await asgi_client.aclose()
            await runtime_manager.stop()
            await env.shutdown()

        asyncio.run_coroutine_threadsafe(_stop_temporal(), asgi_loop).result(
            timeout=15.0
        )
        asgi_loop.call_soon_threadsafe(asgi_loop.stop)
