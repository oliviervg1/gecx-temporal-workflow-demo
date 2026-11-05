#!/usr/bin/env python3
"""End-to-end CLI demonstration runner for CX Agent Studio (GECX) + Temporal.io.

Demonstrates:
  1. Caller ID&V (Claire Vance, card ending 4821, passcode 195501) and silent
     handoff to dispute_specialist_agent, starting DisputeResolutionWorkflow.
  2. Numbered transaction selection (Option 1: £142.50 at Acme Electronics Ltd),
     3-step chargeback eligibility waterfall, and temporary card hold.
  3. Provisional credit (£142.50) + replacement card ending 8914, advancing to
     the 7-day merchant representment timer (AWAITING_MERCHANT_REPRESENTMENT).
  4. Calling back in a brand-new CXAS session while in the 7-day window to verify
     cross-session resume ("Welcome back, Claire Vance. Resuming active dispute case..."),
     followed by fast-forwarding the 7-day timer via Temporal Signal.
  5. Resetting and demonstrating automatic Saga Rollback (caller_abandoned signal)
     releasing the temporary card hold when a caller hangs up mid-dispute.
"""

import argparse
import asyncio
import json
import threading
import time
import urllib.parse
from typing import Any

from demo_ui.server import (
    DEFAULT_ACCOUNT,
    DEFAULT_ADAPTER_URL,
    DEFAULT_APP_NAME,
    DEFAULT_CUSTOMER_ID,
    DEFAULT_CXAS_LOCATION,
    DEFAULT_PROJECT_ID,
    AdapterBridge,
    DemoSessionStore,
    TokenCache,
    boot_embedded_temporal,
    extract_session_variables,
    run_local_controller_turn,
)


def _print_banner(title: str) -> None:
    print(f"\n{'=' * 78}")
    print(f"  {title}")
    print(f"{'=' * 78}")


def _print_turn(
    turn_label: str,
    user_text: str,
    turn_out: dict[str, Any],
    inspect_data: dict[str, Any],
) -> None:
    wf_state = (inspect_data or {}).get("live_state") or {}
    print(f"\n[{turn_label}]")
    print(f"  Caller   : {user_text}")
    print(f"  Agent    : ({turn_out.get('active_agent')}) {turn_out.get('agent_text')}")
    print(
        f"  Workflow : status={wf_state.get('workflow_status')} | "
        f"step_seq={wf_state.get('step_seq')} | "
        f"card_hold={wf_state.get('card_hold_active')} | "
        f"credit=£{wf_state.get('provisional_credit_gbp', 0):.2f} | "
        f"compensated={wf_state.get('saga_compensated')}"
    )


def _execute_turn(
    session_id: str,
    customer_id: str,
    user_text: str,
    store: DemoSessionStore,
    bridge: AdapterBridge,
    app_name: str,
    project_id: str,
    token_cache: TokenCache | None,
    is_first_turn: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = store.get_or_create(session_id, customer_id)
    if app_name and token_cache is not None:
        from cxas_scrapi.core.sessions import Sessions
        from google.oauth2.credentials import Credentials

        creds = Credentials(
            token=token_cache.get_user_token(),
            quota_project_id=project_id,
        )
        sessions_client = Sessions(app_name=app_name, creds=creds)
        seed_vars = (
            {
                "session_state": {
                    "customer_id": customer_id,
                    "conversation_ref": session_id,
                }
            }
            if is_first_turn
            else None
        )
        raw_resp = sessions_client.run(
            session_id=session_id,
            text=user_text,
            variables=seed_vars,
        )
        parsed = sessions_client.get_structured_response(raw_resp)
        updated_vars = extract_session_variables(raw_resp)
        s_dict = state.setdefault("session_state", {})
        if isinstance(updated_vars.get("session_state"), dict):
            s_dict.update(updated_vars["session_state"])
        turn_out = {
            "agent_text": (parsed.get("agent_text") or "").strip(),
            "active_agent": (
                "dispute_specialist_agent"
                if s_dict.get("authenticated")
                else "authentication_agent"
            ),
            "session_variables": dict(s_dict),
        }
    else:
        turn_out = run_local_controller_turn(user_text, state, bridge)

    _, inspect_data = bridge.request(
        "GET", f"/v1/dispute-workflow/{urllib.parse.quote(customer_id)}/inspect"
    )
    return turn_out, inspect_data


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run CXAS + Temporal.io Dispute Resolution Demo"
    )
    parser.add_argument("--project-id", default=DEFAULT_PROJECT_ID)
    parser.add_argument("--cxas-location", default=DEFAULT_CXAS_LOCATION)
    parser.add_argument(
        "--app-name",
        default=DEFAULT_APP_NAME,
        help="Optional full CXAS app resource name for live Cloud execution.",
    )
    parser.add_argument("--adapter-url", default=DEFAULT_ADAPTER_URL)
    parser.add_argument("--customer-id", default=DEFAULT_CUSTOMER_ID)
    parser.add_argument("--account", default=DEFAULT_ACCOUNT)
    parser.add_argument(
        "--embedded-temporal",
        action="store_true",
        help="Start an in-process Temporal + FastAPI ASGI adapter automatically.",
    )
    args = parser.parse_args()

    asgi_loop = None
    asgi_client = None
    env = None
    if args.embedded_temporal:
        asgi_loop = asyncio.new_event_loop()
        loop_thread = threading.Thread(target=asgi_loop.run_forever, daemon=True)
        loop_thread.start()
        env, asgi_client = boot_embedded_temporal(asgi_loop)

    token_cache = TokenCache(account=args.account) if not args.embedded_temporal else None
    bridge = AdapterBridge(
        adapter_url=args.adapter_url,
        token_cache=token_cache,
        asgi_loop=asgi_loop,
        asgi_client=asgi_client,
    )
    store = DemoSessionStore()
    cid = args.customer_id
    run_tag = int(time.time())

    try:
        _print_banner(
            "SCENARIO 1: Live Dispute Journey + Cross-Session Resume + 7-Day Fast-Forward"
        )
        bridge.request("POST", f"/v1/dispute-workflow/{urllib.parse.quote(cid)}/reset", {})

        session_1 = f"demo-call-1-{run_tag}"
        out1, insp1 = _execute_turn(
            session_1,
            cid,
            "My card ends in 4821 and my passcode is 195501.",
            store,
            bridge,
            args.app_name,
            args.project_id,
            token_cache,
            is_first_turn=True,
        )
        _print_turn(
            "Session 1 - Turn 1 (ID&V + Start Workflow)",
            "My card ends in 4821 and my passcode is 195501.",
            out1,
            insp1,
        )

        out2, insp2 = _execute_turn(
            session_1,
            cid,
            "Option 1, the £142.50 payment to Acme Electronics Ltd.",
            store,
            bridge,
            args.app_name,
            args.project_id,
            token_cache,
        )
        _print_turn(
            "Session 1 - Turn 2 (Select Transaction + Waterfall + Card Hold)",
            "Option 1, the £142.50 payment to Acme Electronics Ltd.",
            out2,
            insp2,
        )

        out3, insp3 = _execute_turn(
            session_1,
            cid,
            "Yes, please issue the provisional credit and send my replacement card.",
            store,
            bridge,
            args.app_name,
            args.project_id,
            token_cache,
        )
        _print_turn(
            "Session 1 - Turn 3 (Provisional Credit + Replacement Card 8914)",
            "Yes, please issue the provisional credit and send my replacement card.",
            out3,
            insp3,
        )

        session_2 = f"demo-call-2-{run_tag}"
        out4, insp4 = _execute_turn(
            session_2,
            cid,
            "Hi, calling back to check my active dispute. Card ending 4821, passcode 195501.",
            store,
            bridge,
            args.app_name,
            args.project_id,
            token_cache,
            is_first_turn=True,
        )
        _print_turn(
            "Session 2 (New Call During 7-Day Window - Cross-Session Rehydration)",
            "Hi, calling back to check my active dispute. Card ending 4821, passcode 195501.",
            out4,
            insp4,
        )

        print("\n[Temporal Signal: simulate_merchant_outcome -> MERCHANT_ACCEPTED_LIABILITY]")
        bridge.request(
            "POST",
            "/v1/dispute-workflow/signal",
            {
                "customer_id": cid,
                "signal_name": "simulate_merchant_outcome",
                "payload": "MERCHANT_ACCEPTED_LIABILITY",
            },
        )
        _, insp_after_sig = bridge.request(
            "GET", f"/v1/dispute-workflow/{urllib.parse.quote(cid)}/inspect"
        )
        print(
            "  Workflow : "
            f"status={insp_after_sig['live_state']['workflow_status']} | "
            f"outbound_receipt_sent={insp_after_sig['live_state']['outbound_receipt_sent']}"
        )

        _print_banner("SCENARIO 2: Mid-Call Abandonment + Automatic Saga Rollback")
        bridge.request("POST", f"/v1/dispute-workflow/{urllib.parse.quote(cid)}/reset", {})
        store.clear_customer_sessions(cid)

        session_3 = f"demo-call-3-{run_tag}"
        out5, insp5 = _execute_turn(
            session_3,
            cid,
            "Card ending 4821, passcode 195501.",
            store,
            bridge,
            args.app_name,
            args.project_id,
            token_cache,
            is_first_turn=True,
        )
        _print_turn(
            "Session 3 - Turn 1 (Start New Dispute)",
            "Card ending 4821, passcode 195501.",
            out5,
            insp5,
        )

        out6, insp6 = _execute_turn(
            session_3,
            cid,
            "Option 1 please.",
            store,
            bridge,
            args.app_name,
            args.project_id,
            token_cache,
        )
        _print_turn(
            "Session 3 - Turn 2 (Temporary Hold Placed on Card 4821)",
            "Option 1 please.",
            out6,
            insp6,
        )

        print("\n[Temporal Signal: caller_abandoned -> CALLER_HUNG_UP]")
        bridge.request(
            "POST",
            "/v1/dispute-workflow/signal",
            {
                "customer_id": cid,
                "signal_name": "caller_abandoned",
                "payload": "CALLER_HUNG_UP",
            },
        )
        _, insp_abandoned = bridge.request(
            "GET", f"/v1/dispute-workflow/{urllib.parse.quote(cid)}/inspect"
        )
        st_ab = insp_abandoned["live_state"]
        print(
            f"  Saga Compensation Complete : workflow_status={st_ab['workflow_status']} | "
            f"card_hold_active={st_ab['card_hold_active']} | "
            f"saga_compensated={st_ab['saga_compensated']}"
        )
        print(f"  Step History Tail          : {json.dumps(st_ab['step_history'][-1])}")

        _print_banner(
            "SCENARIO 3: Mid-Workflow Cross-Session Resume + Stale-Step 409 Auto-Resync"
        )
        bridge.request("POST", f"/v1/dispute-workflow/{urllib.parse.quote(cid)}/reset", {})
        store.clear_customer_sessions(cid)

        session_4a = f"demo-call-4a-{run_tag}"
        _execute_turn(
            session_4a,
            cid,
            "Card ending 4821, passcode 195501.",
            store,
            bridge,
            args.app_name,
            args.project_id,
            token_cache,
            is_first_turn=True,
        )
        out_4a_step2, insp_4a_step2 = _execute_turn(
            session_4a,
            cid,
            "Option 1, the £142.50 payment to Acme Electronics Ltd.",
            store,
            bridge,
            args.app_name,
            args.project_id,
            token_cache,
        )
        _print_turn(
            "Session 4A - Stops at CONFIRM_CARD_REPLACEMENT Before Confirming",
            "Option 1, the £142.50 payment to Acme Electronics Ltd.",
            out_4a_step2,
            insp_4a_step2,
        )

        session_4b = f"demo-call-4b-{run_tag}"
        resume_utterance = (
            "Hi, I got disconnected while confirming my card replacement. "
            "Card 4821, passcode 195501."
        )
        out_4b_resume, insp_4b_resume = _execute_turn(
            session_4b,
            cid,
            resume_utterance,
            store,
            bridge,
            args.app_name,
            args.project_id,
            token_cache,
            is_first_turn=True,
        )
        _print_turn(
            "Session 4B - Resumes at CONFIRM_CARD_REPLACEMENT with Welcome Back",
            resume_utterance,
            out_4b_resume,
            insp_4b_resume,
        )

        # Force a stale expected_step_id ('select_transaction') on Session 4B while the
        # workflow is at 'confirm_card_replacement' to verify R1 / _infer_error_code auto-resync.
        st_4b = store.get_or_create(session_4b, cid)
        s_4b = st_4b.setdefault("session_state", {})
        s_4b["expected_step_id"] = "select_transaction"
        s_4b["expected_input_kind"] = "OPTION_SELECT"
        s_4b["workflow_status"] = "SELECT_TRANSACTION"
        s_4b["pending_workflow_options"] = [
            "£142.50 at Acme Electronics Ltd on 2 Nov 2026",
            "£29.99 at StreamFlix Digital on 1 Nov 2026",
        ]
        if args.app_name and token_cache is not None:
            from cxas_scrapi.core.sessions import Sessions
            from google.oauth2.credentials import Credentials

            creds = Credentials(
                token=token_cache.get_user_token(),
                quota_project_id=args.project_id,
            )
            sessions_client = Sessions(app_name=args.app_name, creds=creds)
            raw_stale = sessions_client.run(
                session_id=session_4b,
                text="Option 1, the £142.50 payment at Acme Electronics Ltd.",
                variables={"session_state": dict(s_4b)},
            )
            parsed_stale = sessions_client.get_structured_response(raw_stale)
            updated_stale = extract_session_variables(raw_stale)
            if isinstance(updated_stale.get("session_state"), dict):
                s_4b.update(updated_stale["session_state"])
            out_4b_stale = {
                "agent_text": (parsed_stale.get("agent_text") or "").strip(),
                "active_agent": "dispute_specialist_agent",
                "session_variables": dict(s_4b),
            }
        else:
            out_4b_stale = run_local_controller_turn(
                "Option 1, the £142.50 payment at Acme Electronics Ltd.",
                st_4b,
                bridge,
            )
        _, insp_4b_stale = bridge.request(
            "GET", f"/v1/dispute-workflow/{urllib.parse.quote(cid)}/inspect"
        )
        _print_turn(
            "Session 4B - Stale expected_step_id -> 409 Auto-Resync to 'confirm_card_replacement'",
            "Option 1, the £142.50 payment at Acme Electronics Ltd.",
            out_4b_stale,
            insp_4b_stale,
        )
        resynced_step = out_4b_stale["session_variables"].get("expected_step_id")
        print(f"  Resynced Session expected_step_id : {resynced_step}")

        out_4b_confirm, insp_4b_confirm = _execute_turn(
            session_4b,
            cid,
            "Yes, please block the card and submit the chargeback.",
            store,
            bridge,
            args.app_name,
            args.project_id,
            token_cache,
        )
        _print_turn(
            "Session 4B - Confirm Replacement After 409 Resync",
            "Yes, please block the card and submit the chargeback.",
            out_4b_confirm,
            insp_4b_confirm,
        )
    finally:
        if asgi_loop is not None and asgi_client is not None and env is not None:
            from temporal_service.app import runtime_manager

            async def _cleanup() -> None:
                await asgi_client.aclose()
                await runtime_manager.stop()
                await env.shutdown()

            asyncio.run_coroutine_threadsafe(_cleanup(), asgi_loop).result(timeout=15.0)
            asgi_loop.call_soon_threadsafe(asgi_loop.stop)


if __name__ == "__main__":
    main()
