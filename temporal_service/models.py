"""Typed domain models shared by the FastAPI adapter and Temporal DisputeResolutionWorkflow."""

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class VerifyCallerRequest(BaseModel):
    """Request payload for caller identity verification before entering the dispute specialist."""

    customer_id: str = Field(
        default="customer-claire-bristol",
        description="Unique Albion Bank customer identifier.",
    )
    card_last4: str = Field(
        default="",
        description="Last 4 digits of the customer's Albion Bank debit card.",
    )
    passcode: str = Field(
        default="",
        description="6-digit telephone banking security passcode spoken or keyed by the caller.",
    )


class VerifyCallerResponse(BaseModel):
    """Response payload returned after checking caller credentials and active case status."""

    status: Literal["VERIFIED", "INVALID_CREDENTIALS", "ERROR"] = Field(
        description="Verification outcome."
    )
    customer_id: str = Field(description="Verified customer identifier.")
    customer_name: str = Field(default="", description="Verified customer full name.")
    has_active_case: bool = Field(
        default=False,
        description="Flag indicating whether an active RUNNING dispute workflow exists.",
    )
    active_case_ref: str = Field(
        default="",
        description="Active dispute case reference when a workflow is open.",
    )
    message: str = Field(description="Caller-safe verification status summary.")


class WorkflowInitInput(BaseModel):
    """Initial input passed when starting DisputeResolutionWorkflow via Update-With-Start."""

    customer_id: str = Field(description="Verified Albion Bank customer ID.")
    case_ref: str = Field(description="Dispute case reference identifier.")
    card_last4: str = Field(default="4821", description="Disputed card ending digits.")


class AdvanceDisputeRequest(BaseModel):
    """Generic step request body for syncing, advancing, or cancelling a Temporal workflow."""

    model_config = ConfigDict(extra="forbid")

    customer_id: str = Field(
        default="customer-claire-bristol",
        description="Verified Albion Bank customer ID.",
    )
    case_ref: str = Field(
        default="",
        description="Dispute case reference used to key workflow_id.",
    )
    conversation_ref: str = Field(
        default="conv-default",
        description="CXAS session/conversation correlation identifier.",
    )
    expected_step_id: str = Field(
        default="",
        description="Step ID expected by the CXAS controller for optimistic concurrency.",
    )
    operation: Literal["sync", "submit", "cancel"] = Field(
        default="sync",
        description="Generic workflow operation ('sync', 'submit', or 'cancel').",
    )
    option_index: int = Field(
        default=0,
        description="1-based index into the active step's numbered menu options.",
    )
    confirmed: Literal["", "yes", "no"] = Field(
        default="",
        description="Caller confirmation token ('yes' or 'no') for confirmation steps.",
    )
    input_text: str = Field(
        default="",
        description="Free-text input value for open-ended workflow steps.",
    )
    is_session_resume: bool = Field(
        default=False,
        description="Whether this sync call is resuming an active workflow in a new CXAS session.",
    )


class DisputeStepResponse(BaseModel):
    """Structured state and step-driver contract returned from a Temporal @workflow.update call."""

    status: Literal["SUCCESS", "IN_PROGRESS", "ERROR"] = Field(
        default="SUCCESS",
        description="Transport and execution outcome.",
    )
    error_code: str = Field(
        default="",
        description="Machine-readable error code when status is not SUCCESS.",
    )
    workflow_id: str = Field(description="Deterministic Temporal workflow execution ID.")
    case_ref: str = Field(description="Albion Bank dispute case reference.")
    workflow_status: Literal[
        "SELECT_TRANSACTION",
        "CONFIRM_CARD_REPLACEMENT",
        "AWAITING_MERCHANT_REPRESENTMENT",
        "RESOLVED_CREDITED",
        "CANCELLED_COMPENSATED",
        "ABANDONED_COMPENSATED",
    ] = Field(description="High-level Temporal workflow state machine status.")
    step_id: str = Field(description="Current conversational step identifier.")
    step_seq: int = Field(description="Monotonically increasing workflow step sequence number.")
    input_kind: Literal["NONE", "OPTION_SELECT", "CONFIRMATION", "FREE_TEXT"] = Field(
        default="NONE",
        description="Kind of caller input expected at this workflow step.",
    )
    update_id: str = Field(
        default="",
        description="Step-versioned Temporal Update ID used for within-run deduplication.",
    )
    context_summary: str = Field(
        description="Factual background context folded into agent_action for off-script questions."
    )
    customer_guidance: str = Field(
        description="Caller-ready guidance for the CXAS agent to speak."
    )
    reprompt_guidance: str = Field(
        default="",
        description="Caller-ready re-prompt spoken when input validation fails at this step.",
    )
    end_conversation: bool = Field(
        default=False,
        description="Whether the CXAS session should end after speaking customer_guidance.",
    )
    session_vars: dict[str, Any] = Field(
        default_factory=dict,
        description="Opaque key-value pairs synced into CXAS session state.",
    )
    options: list[str] = Field(
        default_factory=list,
        description="Ordered choices presented to the caller as a 1-based numbered menu.",
    )
    selected_transaction: dict[str, Any] = Field(
        default_factory=dict,
        description="Currently selected disputed transaction details.",
    )
    provisional_credit_gbp: float = Field(
        default=0.0,
        description="Provisional credit amount issued to the customer's account in GBP.",
    )
    card_hold_active: bool = Field(
        default=False,
        description="Whether a temporary security hold is currently staged on the card.",
    )
    saga_compensated: bool = Field(
        default=False,
        description="Whether saga compensation released a previously staged temporary card hold.",
    )
    outbound_receipt_sent: bool = Field(
        default=False,
        description="Whether an out-of-band confirmation receipt was sent for a committed action.",
    )
    next_step_estimated_ms: int = Field(
        default=400,
        description="Estimated latency of the next step in milliseconds for filler scheduling.",
    )


class SignalDisputeRequest(BaseModel):
    """Request payload for sending lifecycle Signals to an active dispute workflow."""

    customer_id: str = Field(default="customer-claire-bristol")
    case_ref: str = Field(default="")
    signal_name: Literal[
        "simulate_merchant_outcome",
        "caller_abandoned",
        "conversation_closed",
    ] = Field(description="Temporal @workflow.signal name.")
    payload: str = Field(
        default="MERCHANT_ACCEPTED_LIABILITY",
        description="Optional signal argument.",
    )


def compute_step_update_id(req: AdvanceDisputeRequest) -> str:
    """Computes a step-versioned Temporal Update ID for idempotent turn execution.

    Avoids the session_id:turn_index anti-pattern where a legitimate second tool call
    in the same turn (with corrected arguments) would return a stale cached response.

    Args:
        req: Incoming AdvanceDisputeRequest from the CXAS controller.

    Returns:
        Deterministic update_id scoped to the conversation, expected step, and args hash.
    """
    canonical_args = f"{req.operation}|{req.option_index}|{req.confirmed}|{req.input_text}"
    args_hash = hashlib.sha256(canonical_args.encode("utf-8")).hexdigest()[:8]
    step_token = req.expected_step_id or "init"
    conv_token = req.conversation_ref or "conv"
    return f"{conv_token}:{step_token}:{args_hash}"
