"""Live ASGI contract tests driving CXAS controller tools against the Step 1 Temporal adapter."""

import asyncio
import threading
from typing import Any

from httpx import ASGITransport, AsyncClient
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

from evals.conftest import (
    AUTH_TOOL_PATH,
    DISPUTE_TOOL_PATH,
    FakeExternalResponse,
    FakeRuntimeContext,
)
from temporal_service.app import app, runtime_manager


class LiveAsgiTemporalTools:
    """Synchronous CXAS `tools` shim that dispatches HTTP calls to the live ASGI app."""

    def __init__(self, loop: asyncio.AbstractEventLoop, client: AsyncClient) -> None:
        self._loop = loop
        self._client = client

    def _post(self, path: str, payload: dict[str, Any]) -> FakeExternalResponse:
        fut = asyncio.run_coroutine_threadsafe(
            self._client.post(path, json=payload), self._loop
        )
        http_resp = fut.result(timeout=10.0)
        return FakeExternalResponse(
            status_code=http_resp.status_code,
            payload=http_resp.json(),
        )

    def temporal_workflow_api_verify_caller(
        self, payload: dict[str, Any]
    ) -> FakeExternalResponse:
        return self._post("/v1/dispute-workflow/verify", payload)

    def temporal_workflow_api_advance_dispute(
        self, payload: dict[str, Any]
    ) -> FakeExternalResponse:
        return self._post("/v1/dispute-workflow/step", payload)


def _load_bound_controllers(
    tools_shim: LiveAsgiTemporalTools,
    initial_state: dict[str, Any] | None = None,
) -> tuple[Any, Any, FakeRuntimeContext]:
    ctx = FakeRuntimeContext(state=dict(initial_state or {}))
    auth_code = AUTH_TOOL_PATH.read_text(encoding="utf-8")
    dispute_code = DISPUTE_TOOL_PATH.read_text(encoding="utf-8")
    auth_ns: dict[str, Any] = {"context": ctx, "tools": tools_shim}
    dispute_ns: dict[str, Any] = {"context": ctx, "tools": tools_shim}
    exec(compile(auth_code, str(AUTH_TOOL_PATH), "exec"), auth_ns)
    exec(compile(dispute_code, str(DISPUTE_TOOL_PATH), "exec"), dispute_ns)
    return auth_ns["authenticate_caller"], dispute_ns["assist_card_dispute"], ctx


def test_cxas_controllers_drive_live_temporal_asgi_adapter_end_to_end() -> None:
    """Drive both CXAS controllers against the real FastAPI + Temporal engine."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    async def _setup() -> tuple[WorkflowEnvironment, AsyncClient]:
        env = await WorkflowEnvironment.start_time_skipping(
            data_converter=pydantic_data_converter
        )
        await runtime_manager.start(existing_client=env.client)
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        return env, client

    async def _teardown(env: WorkflowEnvironment, client: AsyncClient) -> None:
        await client.aclose()
        await runtime_manager.stop()
        await env.shutdown()

    env, client = asyncio.run_coroutine_threadsafe(_setup(), loop).result(timeout=15.0)
    try:
        shim = LiveAsgiTemporalTools(loop, client)

        # Session 1: Verify caller, start workflow, and select Option 1 (Acme Electronics Ltd)
        auth1, dispute1, ctx1 = _load_bound_controllers(shim)
        r_auth = auth1(card_last4="4821", passcode="195501")
        s1 = ctx1.state["session_state"]
        assert s1["authenticated"] is True
        assert s1["has_active_case"] is False
        assert "Claire Vance" in r_auth["agent_action"]

        r_start = dispute1(operation="sync")
        s1 = ctx1.state["session_state"]
        assert s1["expected_step_id"] == "select_transaction"
        assert s1["expected_input_kind"] == "OPTION_SELECT"
        assert s1["workflow_status"] == "SELECT_TRANSACTION"
        assert "Acme Electronics Ltd" in r_start["agent_action"]

        # Select by option_text -> advances to confirm_card_replacement
        r_sel = dispute1(
            operation="submit",
            option_text="Acme Electronics",
        )
        s1 = ctx1.state["session_state"]
        assert s1["expected_step_id"] == "confirm_card_replacement"
        assert s1["expected_input_kind"] == "CONFIRMATION"
        assert s1["workflow_status"] == "CONFIRM_CARD_REPLACEMENT"
        assert s1["card_hold_active"] is True
        assert "temporary security hold on card ending 4821" in r_sel["agent_action"]

        # Session 2 (Cross-Session Callback): fresh CXAS state, same customer_id
        auth2, dispute2, ctx2 = _load_bound_controllers(shim)
        r_auth2 = auth2(card_last4="4821", passcode="195501")
        s2 = ctx2.state["session_state"]
        assert s2["authenticated"] is True
        assert s2["has_active_case"] is True
        assert s2["case_ref"] == "DSP-2026-8841"
        assert "DSP-2026-8841" in r_auth2["agent_action"]

        r_resume = dispute2(operation="sync")
        s2 = ctx2.state["session_state"]
        assert "Welcome back, Claire Vance" in r_resume["agent_action"]
        assert s2["expected_step_id"] == "confirm_card_replacement"
        assert s2["expected_input_kind"] == "CONFIRMATION"
        assert s2["workflow_status"] == "CONFIRM_CARD_REPLACEMENT"

        # Inject a stale step_id in ctx2 to verify 409 StaleStep auto-resync against real adapter
        ctx2.state["session_state"]["expected_step_id"] = "select_transaction"
        ctx2.state["session_state"]["expected_input_kind"] = "OPTION_SELECT"
        ctx2.state["session_state"]["pending_workflow_options"] = [
            "£142.50 at Acme Electronics Ltd on 2 Nov 2026",
            "£29.99 at StreamFlix Digital on 1 Nov 2026",
        ]
        r_resync = dispute2(operation="submit", option_index=1)
        assert "Welcome back" not in r_resync["agent_action"]
        assert ctx2.state["session_state"]["expected_step_id"] == "confirm_card_replacement"

        # Confirm card replacement and chargeback submission
        r_confirm = dispute2(operation="submit", confirmed="yes")
        s2 = ctx2.state["session_state"]
        assert s2["expected_step_id"] == "awaiting_merchant_representment"
        assert s2["expected_input_kind"] == "NONE"
        assert s2["workflow_status"] == "AWAITING_MERCHANT_REPRESENTMENT"
        assert s2["provisional_credit_gbp"] == "142.50"
        assert s2["call_complete"] == "true"
        assert "8914" in r_confirm["agent_action"]
    finally:
        asyncio.run_coroutine_threadsafe(_teardown(env, client), loop).result(
            timeout=15.0
        )
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5.0)
