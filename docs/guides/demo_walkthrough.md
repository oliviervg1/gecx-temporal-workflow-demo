# Demo Walkthrough Guide

This guide walks through the **Albion Bank Card Dispute Resolution** demonstration across both the Split-Screen Architecture Arena web UI and the command-line runner.

## 1. Demo Persona and Routable Test Data

Use the following customer credentials for live and local testing:

| Field | Value | Notes |
| :--- | :--- | :--- |
| **Customer Name** | Claire Vance | Verified Albion Bank current account holder |
| **Customer ID** | `customer-claire-bristol` | Seeded automatically in the Demo UI and CLI runner |
| **Debit Card Last 4 Digits** | `4821` | Albion Bank Visa Debit card |
| **6-Digit Security Passcode** | `195501` | Demo telephone banking passcode |
| **Option 1 Transaction** | `£142.50 at Acme Electronics Ltd on 2 Nov 2026` | Visa Reason Code `13.1` (Merchandise / Services Not Received) |
| **Option 2 Transaction** | `£29.99 at StreamFlix Digital on 1 Nov 2026` | Visa Reason Code `13.2` (Cancelled Recurring Transaction) |
| **Replacement Card Suffix** | `8914` | Issued when the caller confirms card replacement |

> **Security Note:** Spoken six-digit passcodes are used strictly to keep the browser and simulator walkthrough self-contained. Production voice banking deployments use out-of-band mobile push authentication, DTMF keypad capture with transcript redaction, or partial knowledge challenges.

## 2. Launching the Split-Screen Architecture Arena (Local or Cloud Run)

### Option A: 100% Local Execution (`--embedded-temporal`)

Start the Split-Screen Demo UI server locally with an embedded in-process Temporal server and worker:

```bash
uv run python demo_ui/server.py --embedded-temporal --port 8092
```

### Option B: Cloud Run + Live CX Agent Studio Deployment

To deploy the Temporal OpenAPI Adapter, CX Agent Studio application, and IAP-protected Demo UI to your own Google Cloud project:

```bash
export GOOGLE_CLOUD_PROJECT="your-gcp-project-id"
./scripts/deploy_adapter.sh
uv run python scripts/deploy_app.py --project-id "$GOOGLE_CLOUD_PROJECT" --adapter-url "https://gecx-temporal-adapter-<PROJECT_NUMBER>.europe-west1.run.app"
CXAS_APP_NAME="projects/$GOOGLE_CLOUD_PROJECT/locations/eu/apps/<APP_ID>" ./scripts/deploy_ui.sh
```

Open `http://127.0.0.1:8092` in your browser. The interface is split into two equal columns:

- **Left Column (CX Agent Studio Conversation):** Displays the caller and agent conversation, the state-aware **Guided Demo Steps** strip (`1. Verify ID&V (Card 4821 · Passcode 195501)`, `2. Select Option 1 (£142.50 Acme Electronics)`, `3. Confirm Card Block & Credit (Scenario 1)`, `4. Cancel & Saga Hold Release (Scenario 3)`, and `↺ Reset & Start Fresh Dispute`), and the message composer. The strip dynamically highlights the next active step for the current Temporal workflow stage, disables out-of-order steps, and automatically rotates to a fresh CXAS session ID whenever a call completes (`Part.from_end_session`). The top header bar provides **New Session (Simulate Callback)** and **Reset Demo**.
- **Right Column (Temporal.io `DisputeResolutionWorkflow` Inspector):** Polls `/api/workflow/inspect` every `1.5s` to display the interactive three-node call-flow diagram (`1. CALLER & ID&V CLAIMS`, `2. CXAS AGENT & STATE`, `3. TEMPORAL ADAPTER & WF`), the four-step Temporal workflow state pipeline, the three-column metrics strip, the **TEMPORAL SIGNALS** controls (**Fast-Forward Merchant Review (7d)** and **Simulate Caller Hang-Up (Saga Rollback)**), the interactive Node Trace JSON drawer, and the **TEMPORAL SERVER EVENT HISTORY** table.

## 3. Turn-by-Turn Scenario Walkthrough

### Scenario A: Full Dispute Resolution, Cross-Session Resume, and 7-Day Fast-Forward

| Step | Action / Utterance | Active Sub-Agent | Temporal Workflow State | What to Observe |
| :--- | :--- | :--- | :--- | :--- |
| **Turn 1** | Click **1. Verify ID&V (Card 4821 · Passcode 195501)** in the Guided Demo Steps strip | `authentication_agent` → `dispute_specialist_agent` | `SELECT_TRANSACTION` (`step_seq=1`) | `authenticate_caller` verifies Claire Vance, stages a silent handoff to `dispute_specialist_agent`, and triggers `assist_card_dispute(operation="sync")`. Temporal executes `fetch_disputable_transactions` and reads out Options 1 and 2. |
| **Turn 2** | Click **2. Select Option 1 (£142.50 Acme Electronics)** (or type `"Option 2 please, the £29.99 StreamFlix payment on 1 November"`) | `dispute_specialist_agent` | `CONFIRM_CARD_REPLACEMENT` (`step_seq=2`, `card_hold_active=true`) | Temporal executes `evaluate_chargeback_eligibility` and `stage_temporary_card_hold`, placing a reversible security hold on card `4821` and asking Claire to confirm permanent card replacement and provisional credit. |
| **Turn 3** | Click **3. Confirm Card Block & Provisional Credit** | `dispute_specialist_agent` | `AWAITING_MERCHANT_REPRESENTMENT` (`step_seq=3`, `provisional_credit_gbp=142.50`, `outbound_receipt_sent=true`) | Temporal executes `issue_provisional_credit_and_reissue_card` and `send_outbound_dispute_receipt`, credits `£142.50`, orders replacement card ending `8914`, and starts the 7-day merchant representment timer. |
| **Turn 4 (New Call Session)** | Click **New Session (Simulate Callback)** in the top header bar, then click **1. Verify ID&V (Card 4821 · Passcode 195501)** | `authentication_agent` → `dispute_specialist_agent` | `AWAITING_MERCHANT_REPRESENTMENT` (`step_seq=3`, `RUNNING`) | Because the Temporal workflow is `RUNNING`, `/v1/dispute-workflow/verify` returns `has_active_case=true`. The specialist greets Claire with `"Welcome back, Claire Vance. Resuming active dispute case..."` and reports the live 7-day representment status. |
| **Signal & Final Check** | Click **Fast-Forward Merchant Review (7d)** in the right-hand **TEMPORAL SIGNALS** bar | Asynchronous Temporal Signal | `RESOLVED_CREDITED` (`step_seq=4`, `COMPLETED`) | Sends `simulate_merchant_outcome` (`MERCHANT_ACCEPTED_LIABILITY`) to Temporal, waking the 7-day timer and settling the workflow in `RESOLVED_CREDITED`. |

### Scenario B: Caller Abandonment and Automatic Saga Rollback

1. Click **Reset Demo** in the top-right header bar to clear the previous workflow execution.
2. Click **1. Verify ID&V (Card 4821 · Passcode 195501)** to start a new dispute (`SELECT_TRANSACTION`).
3. Click **2. Select Option 1 (£142.50 Acme Electronics)** so Temporal executes `stage_temporary_card_hold` on card `4821` (`CONFIRM_CARD_REPLACEMENT`, `card_hold_active=true`).
4. Click **Simulate Caller Hang-Up (Saga Rollback)** in the right-hand **TEMPORAL SIGNALS** bar (or click **4. Cancel & Trigger Saga Hold Release** in the Guided Demo Steps strip).
5. Observe the right-hand Temporal inspector immediately transition to `ABANDONED_COMPENSATED` (or `CANCELLED_COMPENSATED`), execute the compensating `release_temporary_card_hold` activity (`card_hold_active=false`, `saga_compensated=true`), and record the release in `side_effect_ledger` and `step_history`.
