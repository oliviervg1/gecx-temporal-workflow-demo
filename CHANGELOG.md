# Changelog

All notable changes to this repository are documented in this file.

## [0.2.0] - 2026-11-05

### Changed

- **Consolidated `session_state` (`OBJECT`) Variable & Simplified Callbacks (`cxas_app/` & `demo_ui/`):**
  - **Why:** Declaring 20 flat session variables in `app.json` required maintaining `app.json` whenever new workflow state fields were projected, required JSON string encoding for list/dict fields (`pending_workflow_options`, `pending_in_progress_args`), and included unnecessary `_RESERVED_STATE_KEYS` filtering in `assist_card_dispute` and unused float/int argument repair (`_repair_tool_calls`) in `after_model_callback`.
  - **Fix:** Consolidated all session state into a single `session_state` variable (`type: "OBJECT"`, `"required": []`) in `app.json` using native Python types (`bool`, `int`, `list`, `dict`, `str`), removed `_RESERVED_STATE_KEYS` so Temporal `session_vars` can set or overwrite any key in `session_state`, removed `_repair_tool_calls` from `after_model_callbacks/repair_and_transfer/python_code.py`, and added step-by-step inline comments across `authenticate_caller`, `assist_card_dispute`, and callbacks.
  - **Test:** Verified across `26/26` pytest tests, `cxas lint`, live `cxas test-tools`, and live Golden Evaluations.
  - **Docs:** Updated `cxas_app/README.md`, `docs/tools/README.md`, `docs/tools/authenticate_caller.md`, `docs/tools/assist_card_dispute.md`, and `CHANGELOG.md`.

- **Generic Temporal Step-Driver Protocol (`temporal_service/` & `cxas_app/`):**
  - **Why:** Previously, `assist_card_dispute` hardcoded workflow-specific terminal status names (`_TERMINAL_STATUSES`), step-specific functions (`_run_start_or_status`, `_run_select_transaction`, `_run_confirm_replacement`, `_run_cancel_dispute`), and re-prompt strings inside the CXAS Python tool. Moving step lifecycle metadata into the Temporal response contract makes the CXAS Python controller tool reusable across any multi-step Temporal workflow.
  - **Fix:** Updated `AdvanceDisputeRequest` (`ConfigDict(extra="forbid")`) to accept generic `operation: Literal["sync", "submit", "cancel"]`, `option_index`, `confirmed`, `input_text`, and `is_session_resume`, and enriched `DisputeStepResponse` with `input_kind` (`"NONE"`, `"OPTION_SELECT"`, `"CONFIRMATION"`, `"FREE_TEXT"`), `reprompt_guidance`, `end_conversation`, and `session_vars`. Refactored `assist_card_dispute` into a single generic `_execute_workflow_step` pipeline that validates inputs against `expected_input_kind`, speaks `reprompt_guidance` on validation failures, syncs `session_vars` into `session_state`, and sets `call_complete = "true"` from `end_conversation`.
  - **Test:** Verified across `26/26` pytest unit, replay, contract, and Demo UI HTTP tests (`uv run pytest -v`), `cxas lint`, live `cxas test-tools` (`7/7 PASS`), and live `NAIVE` Golden Evaluations (`4/4 PASS` in both `FAKE` and `REAL` modes).
  - **Docs:** Updated `docs/tools/assist_card_dispute.md`, `docs/tools/README.md`, `docs/architecture/temporal_cxas_integration.md`, `docs/guides/demo_walkthrough.md`, and `cxas_app/README.md`.

## [0.1.0] - 2026-11-04

### Added

- **Durable Temporal Dispute Resolution Service (`temporal_service/`):**
  - **Why:** Retail banking card disputes span both low-latency in-call steps (transaction selection, chargeback eligibility waterfall, temporary card hold, provisional credit, and card replacement) and multi-day asynchronous timers (the seven-day merchant representment window) plus automatic saga compensation if the caller disconnects mid-flow.
  - **Fix:** Implemented `DisputeResolutionWorkflow` using Temporal Python SDK `@workflow.update` (`submit_step` with `@submit_step.validator` `validate_submit_step` rejecting stale or out-of-order steps), `@workflow.query` (`get_live_state`), and `@workflow.signal` (`simulate_merchant_outcome`, `caller_abandoned`, and `conversation_closed`), backed by deterministic Albion Bank activities (`fetch_disputable_transactions`, `evaluate_chargeback_eligibility`, `stage_temporary_card_hold`, `release_temporary_card_hold`, `issue_provisional_credit_and_reissue_card`, and `send_outbound_dispute_receipt`) and a FastAPI OpenAPI 3.0 adapter (`temporal_service/app.py`) with a `2.5s` update execution budget (`IN_PROGRESS` graceful fallback) and deterministic `update_id` deduplication.
  - **Test:** Verified in `tests/test_temporal_workflow.py` against a live `WorkflowEnvironment.start_local` server covering happy path, 7-day signal fast-forward, duplicate `update_id` idempotency, stale `expected_step_id` `HTTP 409` rejection, invalid `option_index` `HTTP 422` rejection, and `caller_abandoned` saga compensation.
  - **Docs:** Documented in `docs/architecture/temporal_cxas_integration.md`.

- **CX Agent Studio (GECX) Two-Agent Application Bundle (`cxas_app/gecx-temporal-workflow-demo/`):**
  - **Why:** Separating caller identity verification (`authentication_agent`) from dispute workflow execution (`dispute_specialist_agent`) isolates authentication boundaries while keeping every agent instruction concise and deterministic.
  - **Fix:** Built `authentication_agent` (`authenticate_caller`) and `dispute_specialist_agent` (`assist_card_dispute`) on `gemini-composite-v2` (`en-GB`) with shared `guard_and_route` (`before_model_callback`) and `repair_and_transfer` (`after_model_callback`), silent `Part.from_agent_transfer` handoff, `0.26s` pre-tool entry filler on specialist entry, `IN_PROGRESS` retry deduplication, cross-session resume greeting (`has_active_case`), and stateful `toolFakeConfig` for offline evaluations.
  - **Test:** Verified in `evals/test_app_schema.py`, `evals/tool_tests/test_controllers.py`, `evals/callback_tests/test_callbacks.py`, and live ASGI contract tests in `tests/test_controller_adapter_contract.py`.
  - **Docs:** Documented in `cxas_app/README.md`, `docs/tools/README.md`, `docs/tools/authenticate_caller.md`, and `docs/tools/assist_card_dispute.md`.

- **Split-Screen Architecture Arena Demo UI & CLI Runner (`demo_ui/` & `scripts/`):**
  - **Why:** Engineers and stakeholders need to inspect both the live conversational stream and the real-time Temporal workflow state machine, activity waterfall, and interactive 3-node call-flow trace side by side, without hitting closed CXAS sessions (`Part.from_end_session`) when testing multi-turn or multi-session scenarios.
  - **Fix:** Created `demo_ui/server.py`, `demo_ui/static/index.html`, and `scripts/demo_temporal_workflow.py` supporting both 100% local `--embedded-temporal` execution and live Cloud Run + CX Agent Studio execution, with state-aware Guided Demo Steps (highlighting the active next step, disabling out-of-order steps, and surfacing a one-click `↺ Reset & Start Fresh Dispute` chip when a case is in the 7-day window or completed) and automatic CXAS `session_id` rotation whenever a call session ends.
  - **Test:** Verified end-to-end in `tests/test_demo_ui.py` (including automatic `session_id` rotation after `session_ended=True`).
  - **Docs:** Documented in `README.md` and `docs/guides/demo_walkthrough.md`.

- **Cloud Run, Direct IAP, and CX Agent Studio Deployment Automation (`scripts/`):**
  - **Why:** Running live CX Agent Studio evaluations and browser sessions requires an OIDC-authenticated Cloud Run Temporal adapter (`--min-instances=1`), automated Gate 3 platform tool verification after `cxas push`, and an IAP-protected Cloud Run Demo UI.
  - **Fix:** Added `scripts/deploy_adapter.sh` (`gecx-temporal-adapter` in `europe-west1`), `scripts/deploy_app.py` (Gate 1 lint, `cxas-scrapi` push to `projects/<PROJECT_ID>/locations/eu/apps/<APP_ID>`, Gate 3 tool/toolset verification, `push-eval` re-sync, and `AppVersion` snapshot creation), and `scripts/deploy_ui.sh` (`gecx-temporal-workflow-demo-ui` with Direct Cloud Run IAP and optional custom OAuth domain bindings), plus canonical gRPC string `errorCode` (`"ABORTED"`) handling in `_infer_error_code` for live OpenAPI `HTTP 409` auto-resync.
  - **Test:** Verified live with `cxas test-tools` (`6/6 PASS` in `evals/tool_tests/controller_tool_tests.yaml`), `NAIVE` Golden Evaluations (`4/4 PASS` in both `FAKE` mode and `REAL` Cloud Run mode), and `scripts/demo_temporal_workflow.py` against the live CXAS app and Cloud Run adapter.
  - **Docs:** Documented in `README.md` and `docs/guides/demo_walkthrough.md`.
