"""Deterministic Temporal Activities simulating Albion Bank backend microservices."""

from typing import Any

from temporalio import activity

_CLAIRE_TRANSACTIONS: list[dict[str, Any]] = [
    {
        "tx_id": "TX-90412",
        "merchant_name": "Acme Electronics Ltd",
        "amount_gbp": 142.50,
        "date": "2 Nov 2026",
        "category": "Retail Electronics",
        "scheme_reason_code": "13.1 — Merchandise / Services Not Received",
        "label": "£142.50 at Acme Electronics Ltd on 2 Nov 2026",
    },
    {
        "tx_id": "TX-90388",
        "merchant_name": "StreamFlix Digital",
        "amount_gbp": 29.99,
        "date": "1 Nov 2026",
        "category": "Digital Subscription",
        "scheme_reason_code": "13.2 — Cancelled Recurring Transaction",
        "label": "£29.99 at StreamFlix Digital on 1 Nov 2026",
    },
]


def _make_claire_profile(customer_id: str) -> dict[str, Any]:
    """Builds a deterministic Claire Vance fixture profile for the given customer_id."""
    return {
        "customer_id": customer_id,
        "customer_name": "Claire Vance",
        "card_last4": "4821",
        "passcode": "195501",
        "default_case_ref": "DSP-2026-8841",
        "transactions": [dict(tx) for tx in _CLAIRE_TRANSACTIONS],
    }


CUSTOMER_DIRECTORY: dict[str, dict[str, Any]] = {
    cid: _make_claire_profile(cid)
    for cid in (
        "customer-claire-bristol",
        "customer-claire-new",
        "customer-claire-resume",
        "customer-claire-decline",
        "customer-claire-stale",
    )
}

# In-memory ledger tracking side effects for test assertions and UI inspection
SIDE_EFFECT_LEDGER: dict[str, dict[str, Any]] = {}


def get_customer_record(customer_id: str) -> dict[str, Any]:
    """Returns the customer profile for a given customer_id (defaulting to Claire Vance)."""
    return CUSTOMER_DIRECTORY.get(
        customer_id, CUSTOMER_DIRECTORY["customer-claire-bristol"]
    )


@activity.defn
async def fetch_disputable_transactions(customer_id: str) -> list[dict[str, Any]]:
    """Fetches recent settled debit-card transactions eligible for dispute."""
    record = get_customer_record(customer_id)
    return list(record["transactions"])


@activity.defn
async def evaluate_chargeback_eligibility(
    customer_id: str, tx_id: str
) -> dict[str, Any]:
    """Executes the 3-step chargeback eligibility waterfall (CMS, Scheme Rules, Duplicates)."""
    record = get_customer_record(customer_id)
    tx = next(
        (t for t in record["transactions"] if t["tx_id"] == tx_id),
        record["transactions"][0],
    )
    return {
        "eligible": True,
        "tx_id": tx["tx_id"],
        "merchant_name": tx["merchant_name"],
        "amount_gbp": tx["amount_gbp"],
        "date": tx["date"],
        "scheme_reason_code": tx["scheme_reason_code"],
        "provisional_credit_eligible": True,
        "replacement_sla_working_days": "3 to 5 UK working days",
        "waterfall_summary": (
            f"Verified settlement for {tx['merchant_name']} "
            f"(£{tx['amount_gbp']:.2f} on {tx['date']}), "
            f"matched Visa scheme rule ({tx['scheme_reason_code']}), "
            "and confirmed zero duplicate claims."
        ),
    }


@activity.defn
async def stage_temporary_card_hold(
    customer_id: str, card_last4: str, idempotency_key: str
) -> dict[str, Any]:
    """Places a reversible temporary security hold on the card pending replacement confirmation."""
    ledger = SIDE_EFFECT_LEDGER.setdefault(customer_id, {})
    ledger["card_hold_active"] = True
    ledger["last_hold_idempotency_key"] = idempotency_key
    return {
        "card_last4": card_last4,
        "card_hold_active": True,
        "idempotency_key": idempotency_key,
    }


@activity.defn
async def release_temporary_card_hold(
    customer_id: str, card_last4: str, reason: str, idempotency_key: str
) -> dict[str, Any]:
    """Saga compensation activity that releases a staged card hold on cancel or abandonment."""
    ledger = SIDE_EFFECT_LEDGER.setdefault(customer_id, {})
    ledger["card_hold_active"] = False
    ledger["saga_compensated"] = True
    ledger["compensation_reason"] = reason
    ledger["last_release_idempotency_key"] = idempotency_key
    return {
        "card_last4": card_last4,
        "card_hold_active": False,
        "saga_compensated": True,
        "reason": reason,
        "idempotency_key": idempotency_key,
    }


@activity.defn
async def issue_provisional_credit_and_reissue_card(
    customer_id: str,
    case_ref: str,
    tx_id: str,
    amount_gbp: float,
    card_last4: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Blocks the compromised card, orders a replacement, and posts provisional credit."""
    ledger = SIDE_EFFECT_LEDGER.setdefault(customer_id, {})
    if ledger.get("credit_idempotency_key") != idempotency_key:
        ledger["credit_idempotency_key"] = idempotency_key
        ledger["provisional_credit_gbp"] = amount_gbp
        ledger["replacement_card_ordered"] = True
        ledger["card_hold_active"] = False
    return {
        "case_ref": case_ref,
        "tx_id": tx_id,
        "provisional_credit_gbp": amount_gbp,
        "replacement_card_last4": "8914",
        "delivery_window": "3 to 5 UK working days",
        "merchant_representment_days": 7,
        "idempotency_key": idempotency_key,
    }


@activity.defn
async def send_outbound_dispute_receipt(
    customer_id: str, case_ref: str, summary: str, idempotency_key: str
) -> dict[str, Any]:
    """Sends an out-of-band SMS/email receipt whenever an irreversible action commits."""
    ledger = SIDE_EFFECT_LEDGER.setdefault(customer_id, {})
    ledger["outbound_receipt_sent"] = True
    ledger["last_receipt_summary"] = summary
    ledger["receipt_idempotency_key"] = idempotency_key
    return {
        "sent": True,
        "channel": "SMS+EMAIL",
        "case_ref": case_ref,
        "idempotency_key": idempotency_key,
    }
