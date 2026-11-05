"""Temporal DisputeResolutionWorkflow with synchronous @workflow.update and saga compensation."""

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from temporal_service.activities import (
        evaluate_chargeback_eligibility,
        fetch_disputable_transactions,
        issue_provisional_credit_and_reissue_card,
        release_temporary_card_hold,
        send_outbound_dispute_receipt,
        stage_temporary_card_hold,
    )
    from temporal_service.models import (
        AdvanceDisputeRequest,
        DisputeStepResponse,
        WorkflowInitInput,
        compute_step_update_id,
    )

CONVERSATIONAL_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(milliseconds=150),
    backoff_coefficient=1.5,
    maximum_interval=timedelta(milliseconds=500),
    maximum_attempts=2,
)
DURABLE_COMPENSATION_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(milliseconds=200),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=5),
    maximum_attempts=5,
)
ACTIVITY_START_TO_CLOSE = timedelta(seconds=2)
ACTIVITY_SCHEDULE_TO_CLOSE = timedelta(milliseconds=2500)
COMPENSATION_SCHEDULE_TO_CLOSE = timedelta(seconds=30)
HUMAN_ABANDONMENT_TIMEOUT = timedelta(minutes=15)
MERCHANT_REPRESENTMENT_WINDOW = timedelta(days=7)


@workflow.defn
class DisputeResolutionWorkflow:
    """Durable multi-turn Albion Bank card dispute workflow driven by CXAS via Updates."""

    def __init__(self) -> None:
        """Initializes workflow state machine variables and synchronization primitives."""
        self._lock = asyncio.Lock()
        self._customer_id: str = "customer-claire-bristol"
        self._case_ref: str = "DSP-2026-8841"
        self._card_last4: str = "4821"
        self._step_seq: int = 0
        self._current_step_id: str = "initializing"
        self._current_input_kind: str = "NONE"
        self._workflow_status: str = "SELECT_TRANSACTION"
        self._transactions: list[dict[str, Any]] = []
        self._selected_tx: dict[str, Any] = {}
        self._eligibility: dict[str, Any] = {}
        self._card_hold_active: bool = False
        self._saga_compensated: bool = False
        self._provisional_credit_gbp: float = 0.0
        self._outbound_receipt_sent: bool = False
        self._pending_request: AdvanceDisputeRequest | None = None
        self._abandoned_signal: bool = False
        self._conversation_closed: bool = False
        self._merchant_outcome: str = ""
        self._current_response: DisputeStepResponse | None = None
        self._step_history: list[dict[str, Any]] = []

    @workflow.run
    async def run(self, init_input: WorkflowInitInput) -> DisputeStepResponse:
        """Executes the deterministic dispute lifecycle across human and async checkpoints."""
        self._customer_id = init_input.customer_id
        self._case_ref = init_input.case_ref
        self._card_last4 = init_input.card_last4

        try:
            # Step 1: Fetch recent disputable transactions and pause at SELECT_TRANSACTION.
            self._transactions = await workflow.execute_activity(
                fetch_disputable_transactions,
                args=[self._customer_id],
                start_to_close_timeout=ACTIVITY_START_TO_CLOSE,
                schedule_to_close_timeout=ACTIVITY_SCHEDULE_TO_CLOSE,
                retry_policy=CONVERSATIONAL_RETRY_POLICY,
            )
            options = [tx["label"] for tx in self._transactions]
            menu_choices = ", or ".join(
                f"{idx + 1} for {label}" for idx, label in enumerate(options)
            )
            self._set_step_state(
                workflow_status="SELECT_TRANSACTION",
                step_id="select_transaction",
                input_kind="OPTION_SELECT",
                context_summary=(
                    f"Case {self._case_ref} opened for card ending {self._card_last4}. "
                    f"Found {len(options)} recent settled transactions eligible for review."
                ),
                customer_guidance=(
                    f"I have opened dispute case {self._case_ref} for your card ending "
                    f"{self._card_last4}. Which recent transaction would you like to dispute: "
                    f"{menu_choices}?"
                ),
                reprompt_guidance=(
                    f"I could not match that selection. Please choose {menu_choices}."
                ),
                end_conversation=False,
                options=options,
                next_step_estimated_ms=900,
            )

            # Wait for the caller to select a transaction (or cancel/abandon).
            try:
                await workflow.wait_condition(
                    lambda: self._pending_request is not None or self._abandoned_signal,
                    timeout=HUMAN_ABANDONMENT_TIMEOUT,
                )
            except asyncio.TimeoutError:
                return await self._finish_abandoned_or_cancelled(abandoned=True)

            if self._abandoned_signal:
                return await self._finish_abandoned_or_cancelled(abandoned=True)

            req = self._consume_pending_request()
            if req.operation == "cancel":
                return await self._finish_abandoned_or_cancelled(abandoned=False)

            idx = req.option_index - 1
            self._selected_tx = self._transactions[idx]

            # Step 2: Run 3-step chargeback eligibility waterfall and stage temporary card hold.
            self._eligibility = await workflow.execute_activity(
                evaluate_chargeback_eligibility,
                args=[self._customer_id, self._selected_tx["tx_id"]],
                start_to_close_timeout=ACTIVITY_START_TO_CLOSE,
                schedule_to_close_timeout=ACTIVITY_SCHEDULE_TO_CLOSE,
                retry_policy=CONVERSATIONAL_RETRY_POLICY,
            )
            await workflow.execute_activity(
                stage_temporary_card_hold,
                args=[
                    self._customer_id,
                    self._card_last4,
                    f"{self._case_ref}:hold:{self._selected_tx['tx_id']}",
                ],
                start_to_close_timeout=ACTIVITY_START_TO_CLOSE,
                schedule_to_close_timeout=ACTIVITY_SCHEDULE_TO_CLOSE,
                retry_policy=CONVERSATIONAL_RETRY_POLICY,
            )
            self._card_hold_active = True

            tx_label = self._selected_tx["label"]
            amount = self._selected_tx["amount_gbp"]
            self._set_step_state(
                workflow_status="CONFIRM_CARD_REPLACEMENT",
                step_id="confirm_card_replacement",
                input_kind="CONFIRMATION",
                context_summary=(
                    f"{self._eligibility['waterfall_summary']} Temporary security hold placed on "
                    f"card ending {self._card_last4}. Provisional credit of £{amount:.2f} ready."
                ),
                customer_guidance=(
                    f"Your selected transaction ({tx_label}) qualifies for an immediate "
                    f"£{amount:.2f} provisional credit under case {self._case_ref}, and I have "
                    f"placed a temporary security hold on card ending {self._card_last4}. Would "
                    "you like me to permanently block that card, order a replacement card ending "
                    "8914 arriving in 3 to 5 UK working days, and submit the chargeback claim?"
                ),
                reprompt_guidance=(
                    "Before I submit the chargeback and issue your provisional credit, "
                    "please confirm with yes or no whether you would like me to permanently "
                    "block your card, order a replacement, and file the claim."
                ),
                end_conversation=False,
                options=[],
                next_step_estimated_ms=1100,
            )

            # Wait up to 15 minutes for caller confirmation; run saga compensation on timeout.
            try:
                await workflow.wait_condition(
                    lambda: self._pending_request is not None or self._abandoned_signal,
                    timeout=HUMAN_ABANDONMENT_TIMEOUT,
                )
            except asyncio.TimeoutError:
                return await self._finish_abandoned_or_cancelled(abandoned=True)

            if self._abandoned_signal:
                return await self._finish_abandoned_or_cancelled(abandoned=True)

            req = self._consume_pending_request()
            if req.operation == "cancel" or req.confirmed != "yes":
                return await self._finish_abandoned_or_cancelled(abandoned=False)

            # Step 3: Commit provisional credit, order replacement card, and send outbound receipt.
            try:
                credit_res = await workflow.execute_activity(
                    issue_provisional_credit_and_reissue_card,
                    args=[
                        self._customer_id,
                        self._case_ref,
                        self._selected_tx["tx_id"],
                        amount,
                        self._card_last4,
                        f"{self._case_ref}:credit:{self._selected_tx['tx_id']}",
                    ],
                    start_to_close_timeout=ACTIVITY_START_TO_CLOSE,
                    schedule_to_close_timeout=ACTIVITY_SCHEDULE_TO_CLOSE,
                    retry_policy=CONVERSATIONAL_RETRY_POLICY,
                )
            except ActivityError:
                return await self._finish_abandoned_or_cancelled(
                    abandoned=False,
                    override_reason="COMMIT_ACTIVITY_FAILED_COMPENSATED",
                )

            self._provisional_credit_gbp = float(credit_res["provisional_credit_gbp"])
            self._card_hold_active = False

            receipt_summary = (
                f"Case {self._case_ref}: £{amount:.2f} provisional credit issued for "
                f"{self._selected_tx['merchant_name']}; replacement card ending "
                f"{credit_res['replacement_card_last4']} dispatched."
            )
            await workflow.execute_activity(
                send_outbound_dispute_receipt,
                args=[
                    self._customer_id,
                    self._case_ref,
                    receipt_summary,
                    f"{self._case_ref}:receipt:{self._selected_tx['tx_id']}",
                ],
                start_to_close_timeout=ACTIVITY_START_TO_CLOSE,
                schedule_to_close_timeout=COMPENSATION_SCHEDULE_TO_CLOSE,
                retry_policy=DURABLE_COMPENSATION_RETRY_POLICY,
            )
            self._outbound_receipt_sent = True

            self._set_step_state(
                workflow_status="AWAITING_MERCHANT_REPRESENTMENT",
                step_id="awaiting_merchant_representment",
                input_kind="NONE",
                context_summary=(
                    f"{receipt_summary} SMS/email confirmation sent. Case is now in the 7-day Visa "
                    "merchant representment window."
                ),
                customer_guidance=(
                    f"I have applied £{amount:.2f} in provisional credit to your account, "
                    f"permanently blocked card ending {self._card_last4}, and ordered your "
                    f"replacement card ending {credit_res['replacement_card_last4']} arriving in "
                    f"3 to 5 UK working days. Dispute case {self._case_ref} is now in the 7-day "
                    "Visa merchant representment window, and we have sent your confirmation "
                    "receipt."
                ),
                reprompt_guidance="",
                end_conversation=True,
                options=[],
                next_step_estimated_ms=400,
            )

            # Step 4: Multi-day async wait for merchant representment (7d or signal).
            try:
                await workflow.wait_condition(
                    lambda: self._merchant_outcome != "",
                    timeout=MERCHANT_REPRESENTMENT_WINDOW,
                )
            except asyncio.TimeoutError:
                self._merchant_outcome = "REPRESENTMENT_WINDOW_ELAPSED_FINAL"

            self._set_step_state(
                workflow_status="RESOLVED_CREDITED",
                step_id="resolved_credited",
                input_kind="NONE",
                context_summary=(
                    f"Case {self._case_ref} permanently settled in the customer's favour "
                    f"({self._merchant_outcome}). The £{amount:.2f} credit for "
                    f"{self._selected_tx['merchant_name']} is now final."
                ),
                customer_guidance=(
                    f"Dispute case {self._case_ref} for {self._selected_tx['merchant_name']} "
                    f"(£{amount:.2f}) has been permanently resolved in your favour, and your "
                    "provisional credit is now final."
                ),
                reprompt_guidance="",
                end_conversation=True,
                options=[],
                next_step_estimated_ms=300,
            )

            await workflow.wait_condition(workflow.all_handlers_finished)
            assert self._current_response is not None
            return self._current_response

        except asyncio.CancelledError:
            if self._card_hold_active:
                await workflow.execute_activity(
                    release_temporary_card_hold,
                    args=[
                        self._customer_id,
                        self._card_last4,
                        "WORKFLOW_CANCELLED",
                        f"{self._case_ref}:release:WORKFLOW_CANCELLED",
                    ],
                    start_to_close_timeout=ACTIVITY_START_TO_CLOSE,
                    schedule_to_close_timeout=COMPENSATION_SCHEDULE_TO_CLOSE,
                    retry_policy=DURABLE_COMPENSATION_RETRY_POLICY,
                )
                self._card_hold_active = False
                self._saga_compensated = True
            raise

    async def _finish_abandoned_or_cancelled(
        self, abandoned: bool, override_reason: str = ""
    ) -> DisputeStepResponse:
        """Executes durable saga compensation if a card hold is active and closes the workflow."""
        reason = override_reason or (
            "CALLER_ABANDONED_TIMEOUT" if abandoned else "CUSTOMER_CANCELLED"
        )
        if self._card_hold_active:
            await workflow.execute_activity(
                release_temporary_card_hold,
                args=[
                    self._customer_id,
                    self._card_last4,
                    reason,
                    f"{self._case_ref}:release:{reason}",
                ],
                start_to_close_timeout=ACTIVITY_START_TO_CLOSE,
                schedule_to_close_timeout=COMPENSATION_SCHEDULE_TO_CLOSE,
                retry_policy=DURABLE_COMPENSATION_RETRY_POLICY,
            )
            self._card_hold_active = False
            self._saga_compensated = True

        status_label = "ABANDONED_COMPENSATED" if abandoned else "CANCELLED_COMPENSATED"
        step_id = "abandoned_compensated" if abandoned else "cancelled_compensated"
        hold_note = (
            f"Saga compensation released the temporary hold on card ending {self._card_last4}."
            if self._saga_compensated
            else f"Card ending {self._card_last4} remains active with no changes."
        )
        self._set_step_state(
            workflow_status=status_label,
            step_id=step_id,
            input_kind="NONE",
            context_summary=f"Dispute case {self._case_ref} closed ({reason}). {hold_note}",
            customer_guidance=(
                f"I have cancelled dispute case {self._case_ref}. Card ending "
                f"{self._card_last4} remains active with any temporary hold removed."
            ),
            reprompt_guidance="",
            end_conversation=True,
            options=[],
            next_step_estimated_ms=300,
        )
        await workflow.wait_condition(workflow.all_handlers_finished)
        assert self._current_response is not None
        return self._current_response

    def _consume_pending_request(self) -> AdvanceDisputeRequest:
        """Pops and returns the pending update request."""
        assert self._pending_request is not None
        req = self._pending_request
        self._pending_request = None
        return req

    def _set_step_state(
        self,
        workflow_status: str,
        step_id: str,
        input_kind: str,
        context_summary: str,
        customer_guidance: str,
        reprompt_guidance: str,
        end_conversation: bool,
        options: list[str],
        next_step_estimated_ms: int,
    ) -> None:
        """Advances step_seq and records a snapshot for the Update return value and UI inspector."""
        self._workflow_status = workflow_status
        self._current_step_id = step_id
        self._current_input_kind = input_kind
        self._step_seq += 1
        self._current_response = DisputeStepResponse(
            status="SUCCESS",
            workflow_id=workflow.info().workflow_id,
            case_ref=self._case_ref,
            workflow_status=workflow_status,  # type: ignore[arg-type]
            step_id=step_id,
            step_seq=self._step_seq,
            input_kind=input_kind,  # type: ignore[arg-type]
            update_id="",
            context_summary=context_summary,
            customer_guidance=customer_guidance,
            reprompt_guidance=reprompt_guidance,
            end_conversation=end_conversation,
            session_vars={
                "workflow_status": workflow_status,
                "card_hold_active": self._card_hold_active,
                "provisional_credit_gbp": f"{self._provisional_credit_gbp:.2f}",
            },
            options=options,
            selected_transaction=dict(self._selected_tx),
            provisional_credit_gbp=self._provisional_credit_gbp,
            card_hold_active=self._card_hold_active,
            saga_compensated=self._saga_compensated,
            outbound_receipt_sent=self._outbound_receipt_sent,
            next_step_estimated_ms=next_step_estimated_ms,
        )
        self._step_history.append(
            {
                "step_seq": self._step_seq,
                "step_id": step_id,
                "workflow_status": workflow_status,
                "timestamp": workflow.now().isoformat(),
                "summary": context_summary,
            }
        )

    def _check_step_and_inputs(self, req: AdvanceDisputeRequest) -> None:
        """Validates step alignment and input parameters against the active step's input_kind."""
        if req.operation == "sync":
            return

        if self._step_seq < 1:
            raise ApplicationError(
                f"Stale step submission: workflow is still initializing Step 1; "
                f"operation '{req.operation}' is not valid before initial sync.",
                type="StaleStep",
                non_retryable=True,
            )

        if not req.expected_step_id:
            raise ApplicationError(
                f"Stale step submission: expected_step_id is required for operation "
                f"'{req.operation}' (current step is '{self._current_step_id}').",
                type="StaleStep",
                non_retryable=True,
            )

        if req.expected_step_id != self._current_step_id:
            raise ApplicationError(
                f"Stale step submission: expected '{self._current_step_id}', "
                f"got '{req.expected_step_id}'.",
                type="StaleStep",
                non_retryable=True,
            )

        if self._current_input_kind == "NONE":
            raise ApplicationError(
                f"Operation '{req.operation}' is not valid at non-interactive step "
                f"'{self._current_step_id}'.",
                type="StaleStep",
                non_retryable=True,
            )

        if req.operation == "cancel":
            return

        if self._current_input_kind == "OPTION_SELECT":
            max_opts = len(self._current_response.options) if self._current_response else 0
            if req.option_index < 1 or req.option_index > max_opts:
                raise ApplicationError(
                    f"Invalid option_index={req.option_index}; expected 1..{max_opts}.",
                    type="InvalidInput",
                    non_retryable=True,
                )
        elif self._current_input_kind == "CONFIRMATION":
            if req.confirmed not in ("yes", "no"):
                raise ApplicationError(
                    f"Invalid confirmed='{req.confirmed}'; expected 'yes' or 'no'.",
                    type="InvalidInput",
                    non_retryable=True,
                )
        elif self._current_input_kind == "FREE_TEXT" and not req.input_text.strip():
            raise ApplicationError(
                "Invalid input_text: non-empty text is required.",
                type="InvalidInput",
                non_retryable=True,
            )

    @workflow.update
    async def submit_step(self, req: AdvanceDisputeRequest) -> DisputeStepResponse:
        """Synchronous Temporal Update handler invoked on each CXAS conversational turn."""
        async with self._lock:
            update_id = compute_step_update_id(req)
            # Wait until Step 1 initialization has finished on Update-With-Start.
            await workflow.wait_condition(lambda: self._step_seq >= 1)

            if req.operation == "sync":
                assert self._current_response is not None
                return self._current_response.model_copy(update={"update_id": update_id})

            # Re-verify inside the lock to eliminate TOCTOU races between concurrent updates.
            self._check_step_and_inputs(req)

            seq_before = self._step_seq
            self._pending_request = req
            await workflow.wait_condition(lambda: self._step_seq > seq_before)
            assert self._current_response is not None
            return self._current_response.model_copy(update={"update_id": update_id})

    @submit_step.validator
    def validate_submit_step(self, req: AdvanceDisputeRequest) -> None:
        """Optimistic concurrency and input validator executed before recording an Update event."""
        self._check_step_and_inputs(req)

    @workflow.signal
    async def simulate_merchant_outcome(self, outcome: str) -> None:
        """Signal that fast-forwards the 7-day merchant representment window in live demos."""
        self._merchant_outcome = outcome or "MERCHANT_ACCEPTED_LIABILITY"

    @workflow.signal
    async def caller_abandoned(self, reason: str = "HANGUP") -> None:
        """Signal that immediately triggers caller abandonment and saga compensation."""
        self._abandoned_signal = True

    @workflow.signal
    async def conversation_closed(self, reason: str = "CALL_COMPLETED") -> None:
        """Lifecycle signal emitted when a CXAS session ends cleanly."""
        self._conversation_closed = True

    @workflow.query
    def get_live_state(self) -> dict[str, Any]:
        """Returns a rich workflow state snapshot for the Split-Screen UI Inspector."""
        return {
            "workflow_id": workflow.info().workflow_id,
            "run_id": workflow.info().run_id,
            "customer_id": self._customer_id,
            "case_ref": self._case_ref,
            "card_last4": self._card_last4,
            "workflow_status": self._workflow_status,
            "step_id": self._current_step_id,
            "step_seq": self._step_seq,
            "selected_transaction": dict(self._selected_tx),
            "provisional_credit_gbp": self._provisional_credit_gbp,
            "card_hold_active": self._card_hold_active,
            "saga_compensated": self._saga_compensated,
            "outbound_receipt_sent": self._outbound_receipt_sent,
            "merchant_outcome": self._merchant_outcome,
            "conversation_closed": self._conversation_closed,
            "current_response": (
                self._current_response.model_dump() if self._current_response else None
            ),
            "step_history": list(self._step_history),
        }
