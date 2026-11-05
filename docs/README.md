# Documentation Index and Glossary

This directory contains the architecture guides, tool specifications, and demonstration walkthroughs for the **Albion Bank Card Dispute Resolution** CX Agent Studio (GECX) + Temporal.io integration.

## 1. Documentation Layout

```text
docs/
├── README.md                                        # Documentation index, role navigation, and domain glossary
├── assets/
│   └── demo_ui_preview.png                          # Split-Screen Architecture Arena screenshot
├── architecture/
│   └── temporal_cxas_integration.md                 # End-to-end Temporal + CXAS architecture, Update/Query/Signal contract, and Saga rollback
├── tools/
│   ├── README.md                                    # Controller tool progression diagram and line-number index
│   ├── authenticate_caller.md                       # Specification for the caller ID&V controller tool
│   └── assist_card_dispute.md                       # Specification for the Temporal workflow step controller tool
└── guides/
    └── demo_walkthrough.md                          # Turn-by-turn demo walkthrough across CLI, Split-Screen UI, and Cloud Run
```

## 2. Navigation by Role

| Role | Recommended Reading Order |
| :--- | :--- |
| **Solution Architects & Engineering Leads** | 1. [README.md](../README.md)<br>2. [docs/architecture/temporal_cxas_integration.md](architecture/temporal_cxas_integration.md)<br>3. [cxas_app/README.md](../cxas_app/README.md) |
| **CX Agent Studio (GECX) Engineers** | 1. [cxas_app/README.md](../cxas_app/README.md)<br>2. [docs/tools/README.md](tools/README.md)<br>3. [docs/tools/authenticate_caller.md](tools/authenticate_caller.md)<br>4. [docs/tools/assist_card_dispute.md](tools/assist_card_dispute.md) |
| **Demo Presenters & QA Testers** | 1. [docs/guides/demo_walkthrough.md](guides/demo_walkthrough.md) |

## 3. Domain Glossary

| Term | Definition |
| :--- | :--- |
| **Chargeback Waterfall** | Three-step automated check performed before accepting a card dispute: fetching posted card transactions, evaluating scheme chargeback eligibility rules, and placing a temporary security hold on the debit card. |
| **CX Agent Studio (GECX)** | Google Cloud's conversational AI platform used to author, evaluate, and run voice and chat agents with sub-agents, Python controller tools, OpenAPI toolsets, and lifecycle callbacks. |
| **ID&V** | Identity and Verification. The security gate where the caller provides the last four digits of their debit card and their six-digit telephone banking passcode before account records are accessed. |
| **Merchant Representment Window** | The seven-day period under Visa and Mastercard scheme rules during which the merchant's acquiring bank can accept liability or submit compelling evidence against a chargeback. |
| **Provisional Credit** | Immediate temporary refund credited to the customer's Albion Bank current account while the seven-day merchant representment window is in progress. |
| **Saga Compensation** | Automatic compensating transaction executed by Temporal (`release_temporary_card_hold`) if the caller cancels the dispute or disconnects after a temporary card hold has been placed but before the formal chargeback is submitted. |
| **Temporal Update (`@workflow.update`)** | Synchronous, strongly typed RPC into a running Temporal workflow that validates the requested state transition, executes activities, mutates durable workflow state, and returns the updated step payload within a single HTTP call. |
