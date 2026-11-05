"""FastAPI OpenAPI Adapter and Embedded Temporal Runtime for CX Agent Studio (GECX)."""

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, HTTPException
from temporalio.api.common.v1 import WorkflowExecution
from temporalio.api.enums.v1 import EventType
from temporalio.api.workflowservice.v1 import DeleteWorkflowExecutionRequest
from temporalio.client import (
    Client,
    WithStartWorkflowOperation,
    WorkflowExecutionStatus,
    WorkflowUpdateFailedError,
    WorkflowUpdateStage,
)
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from temporal_service.activities import (
    SIDE_EFFECT_LEDGER,
    evaluate_chargeback_eligibility,
    fetch_disputable_transactions,
    get_customer_record,
    issue_provisional_credit_and_reissue_card,
    release_temporary_card_hold,
    send_outbound_dispute_receipt,
    stage_temporary_card_hold,
)
from temporal_service.models import (
    AdvanceDisputeRequest,
    DisputeStepResponse,
    SignalDisputeRequest,
    VerifyCallerRequest,
    VerifyCallerResponse,
    WorkflowInitInput,
    compute_step_update_id,
)
from temporal_service.workflows import DisputeResolutionWorkflow

logger = logging.getLogger(__name__)

TASK_QUEUE = os.environ.get("TEMPORAL_TASK_QUEUE", "albion-dispute-task-queue")
UPDATE_WAIT_TIMEOUT_SECONDS = float(os.environ.get("TEMPORAL_UPDATE_TIMEOUT_SEC", "2.5"))

# Tracks demo reset generation per customer so resets cleanly restart even after COMPLETED runs
CUSTOMER_RESET_GEN: dict[str, int] = {}


class TemporalRuntimeManager:
    """Manages the Temporal Client and optional embedded local server + Worker."""

    def __init__(self) -> None:
        """Initializes empty runtime handles."""
        self.env: WorkflowEnvironment | None = None
        self.client: Client | None = None
        self.worker: Worker | None = None
        self._worker_task: asyncio.Task[None] | None = None

    async def start(self, existing_client: Client | None = None) -> None:
        """Starts the Temporal connection and in-process Worker."""
        CUSTOMER_RESET_GEN.clear()
        if existing_client is not None:
            self.client = existing_client
        else:
            temporal_addr = os.environ.get("TEMPORAL_ADDRESS", "").strip()
            namespace = os.environ.get("TEMPORAL_NAMESPACE", "default").strip()
            api_key = os.environ.get("TEMPORAL_API_KEY", "").strip()
            tls_env = os.environ.get("TEMPORAL_TLS", "").strip().lower()
            use_tls = (
                tls_env in ("1", "true", "yes")
                if tls_env
                else bool(api_key)
            )
            db_path = os.environ.get("TEMPORAL_DB_PATH", "/tmp/albion_temporal.db").strip()
            if temporal_addr:
                self.client = await Client.connect(
                    temporal_addr,
                    namespace=namespace,
                    api_key=api_key or None,
                    tls=use_tls,
                    data_converter=pydantic_data_converter,
                )
            else:
                self.env = await WorkflowEnvironment.start_local(
                    data_converter=pydantic_data_converter,
                    dev_server_database_filename=db_path or None,
                )
                self.client = self.env.client

        self.worker = Worker(
            self.client,
            task_queue=TASK_QUEUE,
            workflows=[DisputeResolutionWorkflow],
            activities=[
                fetch_disputable_transactions,
                evaluate_chargeback_eligibility,
                stage_temporary_card_hold,
                release_temporary_card_hold,
                issue_provisional_credit_and_reissue_card,
                send_outbound_dispute_receipt,
            ],
        )
        self._worker_task = asyncio.create_task(self.worker.run())

    async def stop(self) -> None:
        """Gracefully shuts down the Worker and embedded Temporal environment."""
        if self.worker is not None:
            with contextlib.suppress(Exception):
                await self.worker.shutdown()
            self.worker = None
        if self._worker_task is not None:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._worker_task
            self._worker_task = None
        if self.env is not None:
            await self.env.shutdown()
            self.env = None
        self.client = None


runtime_manager = TemporalRuntimeManager()


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan context manager starting and stopping the Temporal runtime."""
    if runtime_manager.client is None:
        await runtime_manager.start()
    try:
        yield
    finally:
        await runtime_manager.stop()


app = FastAPI(
    title="Albion Bank Temporal.io Dispute Workflow Adapter",
    version="1.0.0",
    description="Stateless OpenAPI adapter connecting CX Agent Studio (GECX) to Temporal.io.",
    lifespan=lifespan,
)


def resolve_case_ref(customer_id: str, requested_case_ref: str = "") -> str:
    """Resolves the active dispute case_ref for the customer."""
    if requested_case_ref:
        return requested_case_ref
    record = get_customer_record(customer_id)
    return str(record["default_case_ref"])


def make_workflow_id(customer_id: str, case_ref: str) -> str:
    """Builds the deterministic Temporal workflow ID for a customer dispute case."""
    gen = CUSTOMER_RESET_GEN.get(customer_id, 0)
    suffix = "" if gen == 0 else f":r{gen}"
    return f"dispute:{customer_id}:{case_ref}{suffix}"


def _require_client() -> Client:
    """Returns the connected Temporal Client or raises HTTP 503."""
    if runtime_manager.client is None:
        raise HTTPException(status_code=503, detail="Temporal client is not initialized.")
    return runtime_manager.client


async def _get_workflow_status(
    client: Client, workflow_id: str
) -> WorkflowExecutionStatus | None:
    """Returns the execution status for workflow_id, or None if not found."""
    try:
        handle = client.get_workflow_handle(workflow_id)
        desc = await handle.describe()
        return desc.status
    except Exception:
        return None


@app.get("/health")
@app.get("/healthz")
async def health() -> dict[str, Any]:
    """Health check endpoint for Cloud Run and local readiness probes."""
    return {
        "status": "ok",
        "temporal_connected": runtime_manager.client is not None,
        "task_queue": TASK_QUEUE,
    }


_RESUME_FIXTURE_CUSTOMERS: frozenset[str] = frozenset(
    {
        "customer-claire-resume",
        "customer-claire-decline",
        "customer-claire-stale",
    }
)


async def _ensure_resume_fixture(
    client: Client,
    cid: str = "customer-claire-resume",
    *,
    force_reset: bool = False,
) -> None:
    """Ensures a resume fixture customer has an active workflow at CONFIRM_CARD_REPLACEMENT."""
    case_ref = resolve_case_ref(cid)
    wf_id = make_workflow_id(cid, case_ref)
    if (
        not force_reset
        and await _get_workflow_status(client, wf_id) == WorkflowExecutionStatus.RUNNING
    ):
        return
    await reset_workflow(cid)
    await advance_dispute_step(
        AdvanceDisputeRequest(
            customer_id=cid,
            conversation_ref="conv-resume-seed",
            operation="sync",
        )
    )
    await advance_dispute_step(
        AdvanceDisputeRequest(
            customer_id=cid,
            conversation_ref="conv-resume-seed",
            expected_step_id="select_transaction",
            operation="submit",
            option_index=1,
        )
    )


def _apply_resume_greeting(
    resp: DisputeStepResponse,
    customer_name: str,
    is_session_resume: bool,
) -> DisputeStepResponse:
    """Prepends a cross-session resume greeting when a new CXAS session syncs an active case."""
    if not is_session_resume or resp.status != "SUCCESS":
        return resp
    name = customer_name or "there"
    greeting = f"Welcome back, {name}. Resuming active dispute case {resp.case_ref}. "
    if resp.customer_guidance.startswith("Welcome back,"):
        return resp
    return resp.model_copy(
        update={"customer_guidance": f"{greeting}{resp.customer_guidance}"}
    )


@app.post("/v1/dispute-workflow/verify", response_model=VerifyCallerResponse)
async def verify_caller(req: VerifyCallerRequest) -> VerifyCallerResponse:
    """Verifies caller card_last4 and passcode before disclosing any active dispute state."""
    client = _require_client()
    record = get_customer_record(req.customer_id)

    # Never disclose has_active_case or active_case_ref prior to successful ID&V
    if not req.card_last4 or not req.passcode:
        return VerifyCallerResponse(
            status="INVALID_CREDENTIALS",
            customer_id=record["customer_id"],
            customer_name="",
            has_active_case=False,
            active_case_ref="",
            message="Please provide the last four digits of the card and six-digit passcode.",
        )

    if (
        req.card_last4.strip() == record["card_last4"]
        and req.passcode.strip() == record["passcode"]
    ):
        cid = record["customer_id"]
        if cid in _RESUME_FIXTURE_CUSTOMERS:
            await _ensure_resume_fixture(client, cid, force_reset=True)
        elif cid == "customer-claire-new":
            await reset_workflow(cid)
        case_ref = resolve_case_ref(cid)
        wf_id = make_workflow_id(cid, case_ref)
        wf_status = await _get_workflow_status(client, wf_id)
        has_running_case = wf_status == WorkflowExecutionStatus.RUNNING
        return VerifyCallerResponse(
            status="VERIFIED",
            customer_id=cid,
            customer_name=record["customer_name"],
            has_active_case=has_running_case,
            active_case_ref=case_ref if has_running_case else "",
            message=(
                f"Identity verified for {record['customer_name']}."
                + (
                    f" Active dispute case {case_ref} found."
                    if has_running_case
                    else " Ready to start card dispute review."
                )
            ),
        )

    return VerifyCallerResponse(
        status="INVALID_CREDENTIALS",
        customer_id=record["customer_id"],
        customer_name="",
        has_active_case=False,
        active_case_ref="",
        message="The card digits or six-digit security passcode did not match our records.",
    )


@app.post("/v1/dispute-workflow/step", response_model=DisputeStepResponse)
async def advance_dispute_step(req: AdvanceDisputeRequest) -> DisputeStepResponse:
    """Advances or resumes the customer's Temporal workflow via Update-With-Start."""
    client = _require_client()
    record = get_customer_record(req.customer_id)
    case_ref = resolve_case_ref(record["customer_id"], req.case_ref)
    workflow_id = make_workflow_id(record["customer_id"], case_ref)
    update_id = compute_step_update_id(req)

    existing_handle = client.get_workflow_handle(
        workflow_id, result_type=DisputeStepResponse
    )
    wf_status = await _get_workflow_status(client, workflow_id)

    force_fixture_reset = (
        (req.conversation_ref == "conv-tool-test" and req.operation != "sync")
        or (req.operation == "submit" and req.expected_step_id == "select_transaction")
    )
    if (
        record["customer_id"] in _RESUME_FIXTURE_CUSTOMERS
        and req.conversation_ref != "conv-resume-seed"
        and (wf_status != WorkflowExecutionStatus.RUNNING or force_fixture_reset)
    ):
        await _ensure_resume_fixture(
            client,
            record["customer_id"],
            force_reset=force_fixture_reset,
        )
        case_ref = resolve_case_ref(record["customer_id"], req.case_ref)
        workflow_id = make_workflow_id(record["customer_id"], case_ref)
        existing_handle = client.get_workflow_handle(
            workflow_id, result_type=DisputeStepResponse
        )
        wf_status = await _get_workflow_status(client, workflow_id)

    # 1. If the workflow is already COMPLETED, never reopen it silently.
    if wf_status == WorkflowExecutionStatus.COMPLETED:
        try:
            raw_final = await asyncio.wait_for(
                existing_handle.result(), timeout=UPDATE_WAIT_TIMEOUT_SECONDS
            )
            final_resp = DisputeStepResponse.model_validate(raw_final)
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail=f"Unable to read completed workflow result: {exc}"
            ) from exc
        if req.operation == "sync":
            return final_resp.model_copy(update={"update_id": update_id})
        return final_resp.model_copy(
            update={
                "status": "ERROR",
                "error_code": "CASE_CLOSED",
                "end_conversation": True,
                "update_id": update_id,
                "customer_guidance": (
                    f"Dispute case {case_ref} is already closed "
                    f"({final_resp.workflow_status}) and cannot be modified."
                ),
            }
        )

    # 2. If the workflow is already RUNNING and operation is sync, query live state
    # directly so repeated status checks in the same session never return a stale deduped step.
    if wf_status == WorkflowExecutionStatus.RUNNING and req.operation == "sync":
        try:
            live_state = await asyncio.wait_for(
                existing_handle.query(DisputeResolutionWorkflow.get_live_state),
                timeout=UPDATE_WAIT_TIMEOUT_SECONDS,
            )
            if live_state.get("current_response"):
                resp = DisputeStepResponse(**live_state["current_response"])
                synced = resp.model_copy(update={"update_id": update_id})
                return _apply_resume_greeting(
                    synced, record["customer_name"], req.is_session_resume
                )
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail=f"Workflow query unavailable: {exc}"
            ) from exc

    start_op = WithStartWorkflowOperation(
        DisputeResolutionWorkflow.run,
        WorkflowInitInput(
            customer_id=record["customer_id"],
            case_ref=case_ref,
            card_last4=record["card_last4"],
        ),
        id=workflow_id,
        task_queue=TASK_QUEUE,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
    )

    async def _run_update() -> DisputeStepResponse:
        update_handle = await client.start_update_with_start_workflow(
            DisputeResolutionWorkflow.submit_step,
            req.model_copy(update={"case_ref": case_ref}),
            start_workflow_operation=start_op,
            id=update_id,
            wait_for_stage=WorkflowUpdateStage.ACCEPTED,
        )
        return await update_handle.result()

    try:
        return await asyncio.wait_for(_run_update(), timeout=UPDATE_WAIT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        # Client timeout does NOT cancel the Temporal Update; report accurate live status.
        current_status = "CONFIRM_CARD_REPLACEMENT"
        current_step = req.expected_step_id or "in_progress"
        current_seq = 0
        in_progress_guidance = (
            "I am still processing that step on your dispute case. Please bear with me "
            "for a moment while I check the status."
        )
        with contextlib.suppress(Exception):
            live_state = await existing_handle.query(DisputeResolutionWorkflow.get_live_state)
            if isinstance(live_state.get("current_response"), dict):
                base_resp = DisputeStepResponse(**live_state["current_response"])
                return base_resp.model_copy(
                    update={
                        "status": "IN_PROGRESS",
                        "update_id": update_id,
                        "context_summary": (
                            "Temporal workflow update accepted and still executing."
                        ),
                        "customer_guidance": in_progress_guidance,
                        "next_step_estimated_ms": 1000,
                    }
                )
            current_status = live_state.get("workflow_status", current_status)
            current_step = live_state.get("step_id", current_step)
            current_seq = int(live_state.get("step_seq", 0))
        return DisputeStepResponse(
            status="IN_PROGRESS",
            workflow_id=workflow_id,
            case_ref=case_ref,
            workflow_status=current_status,  # type: ignore[arg-type]
            step_id=current_step,
            step_seq=current_seq,
            update_id=update_id,
            context_summary="Temporal workflow update accepted and still executing.",
            customer_guidance=in_progress_guidance,
            options=[],
            next_step_estimated_ms=1000,
        )
    except WorkflowUpdateFailedError as exc:
        cause = exc.cause
        cause_msg = str(cause) if cause else str(exc)
        err_type = cause.type if isinstance(cause, ApplicationError) else ""
        status_code = 422 if err_type == "InvalidInput" else 409
        raise HTTPException(status_code=status_code, detail=cause_msg) from exc


@app.post("/v1/dispute-workflow/signal")
async def send_workflow_signal(req: SignalDisputeRequest) -> dict[str, Any]:
    """Sends an asynchronous lifecycle Signal to an active Temporal workflow."""
    client = _require_client()
    record = get_customer_record(req.customer_id)
    case_ref = resolve_case_ref(record["customer_id"], req.case_ref)
    workflow_id = make_workflow_id(record["customer_id"], case_ref)
    handle = client.get_workflow_handle(workflow_id)
    try:
        if req.signal_name == "simulate_merchant_outcome":
            await handle.signal(
                DisputeResolutionWorkflow.simulate_merchant_outcome, req.payload
            )
            with contextlib.suppress(Exception):
                await asyncio.wait_for(handle.result(), timeout=3.0)
        elif req.signal_name == "caller_abandoned":
            await handle.signal(DisputeResolutionWorkflow.caller_abandoned, req.payload)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(handle.result(), timeout=3.0)
        elif req.signal_name == "conversation_closed":
            await handle.signal(DisputeResolutionWorkflow.conversation_closed, req.payload)
        return {"status": "SIGNAL_SENT", "workflow_id": workflow_id, "signal": req.signal_name}
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Workflow not active: {exc}") from exc


@app.get("/v1/dispute-workflow/{customer_id}/inspect")
async def inspect_workflow(customer_id: str, case_ref: str = "") -> dict[str, Any]:
    """Returns live Temporal workflow state and formatted event history for the UI inspector."""
    client = _require_client()
    record = get_customer_record(customer_id)
    resolved_ref = resolve_case_ref(record["customer_id"], case_ref)
    workflow_id = make_workflow_id(record["customer_id"], resolved_ref)
    handle = client.get_workflow_handle(workflow_id)

    try:
        desc = await handle.describe()
    except Exception:
        return {
            "exists": False,
            "execution_status": "NOT_STARTED",
            "workflow_id": workflow_id,
            "case_ref": resolved_ref,
            "customer_name": record["customer_name"],
            "card_last4": record["card_last4"],
            "live_state": None,
            "side_effect_ledger": SIDE_EFFECT_LEDGER.get(record["customer_id"], {}),
            "history_events": [],
            "total_history_events": 0,
        }

    live_state: dict[str, Any] | None = None
    with contextlib.suppress(Exception):
        live_state = await handle.query(DisputeResolutionWorkflow.get_live_state)

    history = await handle.fetch_history()
    events: list[dict[str, Any]] = []
    for ev in history.events:
        ev_type = EventType.Name(ev.event_type).replace("EVENT_TYPE_", "")
        if any(
            token in ev_type
            for token in (
                "WORKFLOW_EXECUTION",
                "ACTIVITY_TASK_SCHEDULED",
                "ACTIVITY_TASK_COMPLETED",
                "TIMER_",
                "UPDATE_",
            )
        ):
            events.append(
                {
                    "event_id": ev.event_id,
                    "event_type": ev_type,
                    "timestamp": (
                        ev.event_time.ToJsonString()
                        if ev.HasField("event_time")
                        else ""
                    ),
                }
            )
    return {
        "exists": True,
        "execution_status": desc.status.name if desc.status else "UNKNOWN",
        "workflow_id": workflow_id,
        "case_ref": resolved_ref,
        "customer_name": record["customer_name"],
        "card_last4": record["card_last4"],
        "live_state": live_state,
        "side_effect_ledger": SIDE_EFFECT_LEDGER.get(record["customer_id"], {}),
        "history_events": events[-18:],
        "total_history_events": len(history.events),
    }


@app.post("/v1/dispute-workflow/{customer_id}/reset")
async def reset_workflow(customer_id: str, case_ref: str = "") -> dict[str, Any]:
    """Terminates/deletes any workflow for the customer and increments the demo reset counter."""
    client = _require_client()
    record = get_customer_record(customer_id)
    cid = record["customer_id"]
    resolved_ref = resolve_case_ref(cid, case_ref)
    workflow_id = make_workflow_id(cid, resolved_ref)
    SIDE_EFFECT_LEDGER.pop(cid, None)

    handle = client.get_workflow_handle(workflow_id)
    run_id = ""
    with contextlib.suppress(Exception):
        desc = await handle.describe()
        run_id = desc.run_id or ""
    with contextlib.suppress(Exception):
        await handle.terminate(reason="Demo UI Reset")
    with contextlib.suppress(Exception):
        await client.workflow_service.delete_workflow_execution(
            DeleteWorkflowExecutionRequest(
                namespace=client.namespace,
                workflow_execution=WorkflowExecution(
                    workflow_id=workflow_id,
                    run_id=run_id,
                ),
            )
        )

    if await _get_workflow_status(client, workflow_id) is None:
        CUSTOMER_RESET_GEN.pop(cid, None)
    else:
        CUSTOMER_RESET_GEN[cid] = CUSTOMER_RESET_GEN.get(cid, 0) + 1
    next_ref = resolve_case_ref(cid)
    return {
        "status": "RESET",
        "terminated_workflow_id": workflow_id,
        "next_case_ref": next_ref,
        "next_workflow_id": make_workflow_id(cid, next_ref),
    }
