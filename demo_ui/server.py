#!/usr/bin/env python3
"""Split-Screen Demo UI Server for CX Agent Studio (GECX) + Temporal.io Workflow PoC.

Connects the browser UI (http://127.0.0.1:8092 locally or Cloud Run behind IAP) to:
  1. Deployed CX Agent Studio (GECX) app in locations/eu (or the local controller
     pipeline when running in offline/local mode).
  2. Temporal.io Dispute Workflow Adapter (/v1/dispute-workflow/*) on Cloud Run or
     in-process ASGI.

Endpoints:
  - GET  /                          -> Serves static/index.html
  - GET  /health                    -> Health check for Cloud Run
  - GET  /api/config                -> Returns app, adapter URL, customer metadata
  - GET  /api/workflow/inspect      -> Proxies GET /v1/dispute-workflow/{customer_id}/inspect
  - POST /api/session/turn          -> Runs a conversational turn + workflow inspect
  - POST /api/workflow/signal       -> Proxies POST /v1/dispute-workflow/signal
  - POST /api/workflow/reset        -> Proxies POST /v1/dispute-workflow/{customer_id}/reset
"""

import argparse
import asyncio
import http.server
import json
import logging
import mimetypes
import os
import re
import socketserver
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import google.auth
import google.auth.transport.requests
from cxas_scrapi.core.sessions import Sessions
from google.oauth2 import id_token as google_id_token
from google.oauth2.credentials import Credentials

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
APP_BUNDLE_DIR = REPO_ROOT / "cxas_app" / "gecx-temporal-workflow-demo"
AUTH_TOOL_PATH = (
    APP_BUNDLE_DIR / "tools" / "authenticate_caller" / "python_function" / "python_code.py"
)
DISPUTE_TOOL_PATH = (
    APP_BUNDLE_DIR / "tools" / "assist_card_dispute" / "python_function" / "python_code.py"
)

DEFAULT_PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "your-gcp-project-id")
DEFAULT_CXAS_LOCATION = os.environ.get("CXAS_LOCATION", "eu")
DEFAULT_APP_NAME = os.environ.get("CXAS_APP_NAME", "")
DEFAULT_ADAPTER_URL = os.environ.get("TEMPORAL_ADAPTER_URL", "http://127.0.0.1:8080")
DEFAULT_ACCOUNT = os.environ.get("GCLOUD_ACCOUNT", "")
DEFAULT_CUSTOMER_ID = os.environ.get("DEFAULT_CUSTOMER_ID", "customer-claire-bristol")
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
MAX_REQUEST_BODY_BYTES = 65_536
MAX_STORED_SESSIONS = 500


class TokenCache:
    """Caches OAuth2 access tokens (for CXAS) and OIDC ID tokens (for Cloud Run Adapter)."""

    def __init__(self, account: str) -> None:
        self.account = account
        self._adc_creds: Any = None
        self._user_token: str = ""
        self._user_exp: float = 0.0
        self._id_tokens: dict[str, tuple[str, float]] = {}

    def get_user_token(self) -> str:
        """Return a valid OAuth2 access token for CX Agent Studio API calls."""
        if os.environ.get("K_SERVICE"):
            if self._adc_creds is None:
                self._adc_creds, _ = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
            if not self._adc_creds.valid:
                self._adc_creds.refresh(google.auth.transport.requests.Request())
            return str(self._adc_creds.token or "").strip()

        now = time.time()
        if self._user_token and now < self._user_exp:
            return self._user_token

        cmd = ["gcloud", "auth", "print-access-token"]
        if self.account:
            cmd.append(f"--account={self.account}")
        self._user_token = subprocess.run(
            cmd, check=True, capture_output=True, text=True
        ).stdout.strip()
        self._user_exp = now + 1800.0
        return self._user_token

    def get_adapter_id_token(self, adapter_url: str) -> str:
        """Return an OIDC ID token when calling an https://*.run.app Cloud Run adapter."""
        clean_url = adapter_url.rstrip("/")
        if not clean_url.startswith("https://"):
            return ""
        now = time.time()
        cached = self._id_tokens.get(clean_url)
        if cached and now < cached[1]:
            return cached[0]

        if os.environ.get("K_SERVICE"):
            req = google.auth.transport.requests.Request()
            tok = google_id_token.fetch_id_token(req, clean_url)
            self._id_tokens[clean_url] = (tok, now + 1800.0)
            return tok

        cmd = ["gcloud", "auth", "print-identity-token"]
        if self.account:
            cmd.append(f"--account={self.account}")
        tok = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()
        self._id_tokens[clean_url] = (tok, now + 1800.0)
        return tok


class AdapterBridge:
    """Dispatches HTTP requests to a remote Temporal Adapter URL or in-process ASGI app."""

    def __init__(
        self,
        adapter_url: str,
        token_cache: TokenCache | None = None,
        asgi_loop: asyncio.AbstractEventLoop | None = None,
        asgi_client: Any = None,
    ) -> None:
        self.adapter_url = adapter_url.rstrip("/")
        self.token_cache = token_cache
        self.asgi_loop = asgi_loop
        self.asgi_client = asgi_client

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """Execute GET or POST against the Temporal Adapter and return (status_code, body)."""
        if self.asgi_client is not None and self.asgi_loop is not None:
            fut = asyncio.run_coroutine_threadsafe(
                self._asgi_request(method, path, payload), self.asgi_loop
            )
            return fut.result(timeout=15.0)

        url = f"{self.adapter_url}{path}"
        headers = {"Content-Type": "application/json"}
        if self.token_cache is not None and self.adapter_url.startswith("https://"):
            id_tok = self.token_cache.get_adapter_id_token(self.adapter_url)
            if id_tok:
                headers["Authorization"] = f"Bearer {id_tok}"

        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8") or "{}"
                return resp.status, json.loads(raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8") if exc.fp else "{}"
            try:
                err_body = json.loads(raw or "{}")
            except ValueError:
                err_body = {"detail": raw}
            return exc.code, err_body

    async def _asgi_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
    ) -> tuple[int, dict[str, Any]]:
        if method == "GET":
            resp = await self.asgi_client.get(path)
        else:
            resp = await self.asgi_client.post(path, json=payload or {})
        return resp.status_code, resp.json()


class _BridgeExternalResponse:
    """ExternalResponse adapter wrapping AdapterBridge calls for local controller execution."""

    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self.payload = payload

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def text(self) -> str:
        return json.dumps(self.payload) if self.payload else ""

    def json(self) -> dict[str, Any]:
        return self.payload


class _BridgeToolsNamespace:
    """Provides tools.temporal_workflow_api_* methods backed by AdapterBridge."""

    def __init__(self, bridge: AdapterBridge) -> None:
        self.bridge = bridge
        self.api_exchanges: list[dict[str, Any]] = []

    def temporal_workflow_api_verify_caller(
        self, payload: dict[str, Any]
    ) -> _BridgeExternalResponse:
        status, body = self.bridge.request("POST", "/v1/dispute-workflow/verify", payload)
        self.api_exchanges.append(
            {
                "operation": "temporal_workflow_api_verify_caller",
                "endpoint": "POST /v1/dispute-workflow/verify",
                "request": dict(payload),
                "status_code": status,
                "response": dict(body),
            }
        )
        return _BridgeExternalResponse(status, body)

    def temporal_workflow_api_advance_dispute(
        self, payload: dict[str, Any]
    ) -> _BridgeExternalResponse:
        status, body = self.bridge.request("POST", "/v1/dispute-workflow/step", payload)
        self.api_exchanges.append(
            {
                "operation": "temporal_workflow_api_advance_dispute",
                "endpoint": "POST /v1/dispute-workflow/step",
                "request": dict(payload),
                "status_code": status,
                "response": dict(body),
            }
        )
        return _BridgeExternalResponse(status, body)


class _LocalContext:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state


def run_local_controller_turn(
    user_text: str,
    session_state: dict[str, Any],
    bridge: AdapterBridge,
) -> dict[str, Any]:
    """Execute the Two-Agent Handoff Topology locally against the real Temporal Adapter."""
    ctx = _LocalContext(session_state)
    tools_ns = _BridgeToolsNamespace(bridge)
    auth_ns: dict[str, Any] = {"context": ctx, "tools": tools_ns}
    dispute_ns: dict[str, Any] = {"context": ctx, "tools": tools_ns}
    exec(compile(AUTH_TOOL_PATH.read_text("utf-8"), str(AUTH_TOOL_PATH), "exec"), auth_ns)
    exec(
        compile(DISPUTE_TOOL_PATH.read_text("utf-8"), str(DISPUTE_TOOL_PATH), "exec"),
        dispute_ns,
    )
    auth_fn = auth_ns["authenticate_caller"]
    dispute_fn = dispute_ns["assist_card_dispute"]

    tool_calls: list[dict[str, Any]] = []
    tool_responses: list[dict[str, Any]] = []
    agent_transitions: list[str] = []

    s = session_state.setdefault("session_state", {})
    if not s.get("authenticated"):
        active_agent = "authentication_agent"
        agent_transitions.append("authentication_agent")
        digits = re.findall(r"\b\d{4,6}\b", user_text)
        card_last4 = next((d for d in digits if len(d) == 4), "")
        passcode = next((d for d in digits if len(d) == 6), "")
        call_args = {"card_last4": card_last4, "passcode": passcode}
        tool_calls.append({"name": "authenticate_caller", "args": call_args})
        auth_res = auth_fn(**call_args)
        tool_responses.append({"name": "authenticate_caller", "response": auth_res})

        s = session_state.setdefault("session_state", {})
        if s.get("target_agent") == "dispute_specialist_agent":
            s["target_agent"] = ""
            active_agent = "dispute_specialist_agent"
            agent_transitions.append("dispute_specialist_agent")
            tool_calls.append(
                {
                    "name": "transfer_to_agent",
                    "args": {"agent": "dispute_specialist_agent"},
                }
            )
            if s.get("pending_dispute_init") is True:
                s["pending_dispute_init"] = False
                session_state["session_state"] = s
                init_args = {"operation": "sync"}
                tool_calls.append({"name": "assist_card_dispute", "args": init_args})
                init_res = dispute_fn(**init_args)
                tool_responses.append({"name": "assist_card_dispute", "response": init_res})
                return {
                    "agent_text": init_res["agent_action"],
                    "active_agent": active_agent,
                    "agent_transitions": agent_transitions,
                    "tool_calls": tool_calls,
                    "tool_responses": tool_responses,
                    "api_exchanges": tools_ns.api_exchanges,
                    "session_variables": dict(session_state.get("session_state") or {}),
                }
            session_state["session_state"] = s

        return {
            "agent_text": auth_res["agent_action"],
            "active_agent": active_agent,
            "agent_transitions": agent_transitions,
            "tool_calls": tool_calls,
            "tool_responses": tool_responses,
            "api_exchanges": tools_ns.api_exchanges,
            "session_variables": dict(session_state.get("session_state") or {}),
        }

    active_agent = "dispute_specialist_agent"
    agent_transitions.append("dispute_specialist_agent")
    lower = user_text.lower()
    input_kind = s.get("expected_input_kind", "")

    opt_match = re.search(
        r"\b(?:option|choice|number)\s*([12])\b|^\s*([12])(?:\b|$)",
        lower,
    )
    explicit_opt = int(opt_match.group(1) or opt_match.group(2)) if opt_match else 0
    option_text = (
        "Acme Electronics Ltd"
        if "acme" in lower
        else ("StreamFlix Digital" if "streamflix" in lower else "")
    )

    if any(w in lower for w in ("cancel", "never mind", "keep my card", "remove the hold")):
        if input_kind == "CONFIRMATION" and "no" in lower:
            call_args = {"operation": "submit", "confirmed": "no"}
        else:
            call_args = {"operation": "cancel"}
    elif input_kind == "CONFIRMATION" and re.search(r"\bno\b", lower):
        call_args = {"operation": "submit", "confirmed": "no"}
    elif input_kind == "CONFIRMATION" and any(
        w in lower for w in ("yes", "confirm", "block", "replace", "go ahead", "submit")
    ):
        call_args = {"operation": "submit", "confirmed": "yes"}
    elif input_kind == "OPTION_SELECT" or explicit_opt or option_text:
        call_args = {
            "operation": "submit",
            "option_index": explicit_opt,
            "option_text": option_text,
        }
    else:
        call_args = {"operation": "sync"}

    tool_calls.append({"name": "assist_card_dispute", "args": call_args})
    dispute_res = dispute_fn(**call_args)
    tool_responses.append({"name": "assist_card_dispute", "response": dispute_res})
    return {
        "agent_text": dispute_res["agent_action"],
        "active_agent": active_agent,
        "agent_transitions": agent_transitions,
        "tool_calls": tool_calls,
        "tool_responses": tool_responses,
        "api_exchanges": tools_ns.api_exchanges,
        "session_variables": dict(session_state.get("session_state") or {}),
    }


def _to_plain_data(val: Any) -> Any:
    """Recursively convert proto-plus composite objects into plain Python dicts/lists."""
    if isinstance(val, (str, int, float, bool)) or val is None:
        return val
    if hasattr(val, "items"):
        return {str(k): _to_plain_data(v) for k, v in val.items()}
    if hasattr(val, "__iter__"):
        return [_to_plain_data(v) for v in val]
    return val


def extract_session_variables(raw_response: Any) -> dict[str, Any]:
    """Extract updated CXAS session variables from diagnostic chunks."""
    updated_vars: dict[str, Any] = {}
    if raw_response is None:
        return updated_vars
    try:
        outputs = raw_response.outputs or []
    except AttributeError:
        outputs = []
    for output in outputs:
        try:
            messages = output.diagnostic_info.messages or []
        except AttributeError:
            messages = []
        for message in messages:
            try:
                chunks = message.chunks or []
            except AttributeError:
                chunks = []
            for chunk in chunks:
                try:
                    chunk_vars = chunk.updated_variables
                except AttributeError:
                    chunk_vars = None
                if chunk_vars:
                    updated_vars.update(_to_plain_data(dict(chunk_vars)))
    return updated_vars


class DemoSessionStore:
    """Thread-safe store tracking per-session CXAS variables and recent turns."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._ended_sessions: set[str] = set()
        self._rotation_counts: dict[str, int] = {}
        self._recent_turns: dict[str, list[dict[str, Any]]] = {}

    def get_or_create(self, session_id: str, customer_id: str) -> dict[str, Any]:
        with self._lock:
            if session_id not in self._sessions:
                if len(self._sessions) >= MAX_STORED_SESSIONS:
                    oldest_sid = next(iter(self._sessions))
                    self._sessions.pop(oldest_sid, None)
                    self._ended_sessions.discard(oldest_sid)
                    self._rotation_counts.pop(oldest_sid, None)
                self._sessions[session_id] = {
                    "session_state": {
                        "customer_id": customer_id,
                        "customer_name": "",
                        "authenticated": False,
                        "auth_attempts": 0,
                        "case_ref": "",
                        "conversation_ref": session_id,
                        "expected_step_id": "",
                        "expected_input_kind": "",
                        "reprompt_guidance": "",
                        "has_active_case": False,
                        "workflow_id": "",
                        "workflow_status": "",
                        "pending_workflow_options": [],
                        "pending_in_progress_args": {},
                        "card_hold_active": False,
                        "provisional_credit_gbp": "0.00",
                        "target_agent": "",
                        "pending_dispute_init": False,
                        "call_complete": "",
                        "escalation_reason": "",
                    }
                }
            return self._sessions[session_id]

    def is_session_ended(self, session_id: str) -> bool:
        with self._lock:
            if session_id in self._ended_sessions:
                return True
            state = self._sessions.get(session_id)
            s = (state or {}).get("session_state") or {}
            if s.get("call_complete") in ("true", "released"):
                return True
            return False

    def mark_session_ended(self, session_id: str) -> None:
        with self._lock:
            self._ended_sessions.add(session_id)

    def rotate_session_id(self, base_session_id: str) -> str:
        with self._lock:
            next_idx = self._rotation_counts.get(base_session_id, 0) + 1
            self._rotation_counts[base_session_id] = next_idx
            return f"{base_session_id}-r{next_idx}"

    def record_turn(self, customer_id: str, turn_record: dict[str, Any]) -> None:
        with self._lock:
            turns = self._recent_turns.setdefault(customer_id, [])
            turns.append(turn_record)
            if len(turns) > 20:
                del turns[:-20]

    def get_recent_turns(self, customer_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._recent_turns.get(customer_id, []))

    def clear_customer_sessions(self, customer_id: str) -> None:
        with self._lock:
            to_remove = [
                sid
                for sid, st in self._sessions.items()
                if (st.get("session_state") or {}).get("customer_id") == customer_id
            ]
            for sid in to_remove:
                self._sessions.pop(sid, None)
                self._ended_sessions.discard(sid)
                self._rotation_counts.pop(sid, None)
            self._recent_turns.pop(customer_id, None)


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Multi-threaded HTTP server so UI polling and turns never block each other."""

    daemon_threads = True
    allow_reuse_address = True


def make_handler(
    bridge: AdapterBridge,
    token_cache: TokenCache | None = None,
    project_id: str = DEFAULT_PROJECT_ID,
    cxas_location: str = DEFAULT_CXAS_LOCATION,
    app_name: str = DEFAULT_APP_NAME,
    session_store: DemoSessionStore | None = None,
) -> type[http.server.BaseHTTPRequestHandler]:
    """Create the HTTP request handler bound to the Temporal AdapterBridge and optional CXAS app."""
    store = session_store or DemoSessionStore()

    class DemoUIHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            logger.debug(fmt, *args)

        def _send_json(self, status: int, data: dict[str, Any]) -> None:
            raw = json.dumps(data).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _serve_static(self, rel_path: str) -> None:
            target = (STATIC_DIR / rel_path).resolve()
            if not target.is_relative_to(STATIC_DIR.resolve()) or not target.is_file():
                self.send_error(404, "File not found")
                return
            mime, _ = mimetypes.guess_type(str(target))
            raw = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime or "application/octet-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path in ("/health", "/healthz"):
                self._send_json(200, {"status": "ok", "app_name": app_name})
                return
            if parsed.path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if parsed.path in ("/", "/index.html"):
                self._serve_static("index.html")
                return
            if parsed.path.startswith("/static/"):
                self._serve_static(parsed.path.removeprefix("/static/"))
                return
            if parsed.path == "/api/config":
                self._send_json(
                    200,
                    {
                        "project_id": project_id,
                        "cxas_location": cxas_location,
                        "app_name": app_name or "local-controller-runtime",
                        "adapter_url": bridge.adapter_url,
                        "default_customer_id": DEFAULT_CUSTOMER_ID,
                        "customer_name": "Claire Vance",
                        "card_last4": "4821",
                        "passcode_hint": "195501",
                    },
                )
                return
            if parsed.path == "/api/workflow/inspect":
                qs = urllib.parse.parse_qs(parsed.query)
                cid = (qs.get("customer_id", [DEFAULT_CUSTOMER_ID])[0] or "").strip()
                case_ref = (qs.get("case_ref", [""])[0] or "").strip()
                path = f"/v1/dispute-workflow/{urllib.parse.quote(cid)}/inspect"
                if case_ref:
                    path += f"?case_ref={urllib.parse.quote(case_ref)}"
                status, data = bridge.request("GET", path)
                data["recent_turns"] = store.get_recent_turns(cid)
                self._send_json(status, data)
                return
            self.send_error(404, "Not found")

        def do_POST(self) -> None:  # noqa: N802
            content_type = (self.headers.get("Content-Type") or "").lower()
            if content_type and not content_type.startswith("application/json"):
                self._send_json(415, {"error": "Content-Type must be application/json"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > MAX_REQUEST_BODY_BYTES:
                    self._send_json(413, {"error": "Request body too large"})
                    return
                raw_body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
                body = json.loads(raw_body or "{}")
            except ValueError:
                self._send_json(400, {"error": "Invalid JSON body"})
                return

            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/session/turn":
                self._handle_session_turn(body)
                return
            if parsed.path == "/api/workflow/signal":
                cid = (body.get("customer_id") or DEFAULT_CUSTOMER_ID).strip()
                signal_name = (body.get("signal_name") or "simulate_merchant_outcome").strip()
                sig_payload = (body.get("payload") or "MERCHANT_ACCEPTED_LIABILITY").strip()
                status, data = bridge.request(
                    "POST",
                    "/v1/dispute-workflow/signal",
                    {
                        "customer_id": cid,
                        "case_ref": (body.get("case_ref") or "").strip(),
                        "signal_name": signal_name,
                        "payload": sig_payload,
                    },
                )
                _, inspect_data = bridge.request(
                    "GET", f"/v1/dispute-workflow/{urllib.parse.quote(cid)}/inspect"
                )
                self._send_json(
                    status,
                    {"signal_result": data, "workflow_inspect": inspect_data},
                )
                return
            if parsed.path == "/api/workflow/reset":
                cid = (body.get("customer_id") or DEFAULT_CUSTOMER_ID).strip()
                store.clear_customer_sessions(cid)
                status, data = bridge.request(
                    "POST",
                    f"/v1/dispute-workflow/{urllib.parse.quote(cid)}/reset",
                    {},
                )
                _, inspect_data = bridge.request(
                    "GET", f"/v1/dispute-workflow/{urllib.parse.quote(cid)}/inspect"
                )
                self._send_json(
                    status,
                    {"reset_result": data, "workflow_inspect": inspect_data},
                )
                return
            self.send_error(404, "Not found")

        def _handle_session_turn(self, body: dict[str, Any]) -> None:
            cid = (body.get("customer_id") or DEFAULT_CUSTOMER_ID).strip()
            session_id = (body.get("session_id") or "").strip()
            text = (body.get("text") or "").strip()
            force_local = bool(body.get("use_local_runtime", False))
            if not session_id or not text:
                self._send_json(400, {"error": "session_id and text are required"})
                return

            t0 = time.time()
            effective_session_id = session_id
            is_first_turn = bool(body.get("is_first_turn"))
            session_rotated = False
            if store.is_session_ended(effective_session_id):
                effective_session_id = store.rotate_session_id(session_id)
                is_first_turn = True
                session_rotated = True

            state = store.get_or_create(effective_session_id, cid)
            try:
                if app_name and not force_local and token_cache is not None:
                    turn_out = self._run_live_cxas_turn(
                        effective_session_id,
                        session_id,
                        cid,
                        text,
                        state,
                        is_first_turn,
                    )
                    effective_session_id = str(
                        turn_out.get("session_id") or effective_session_id
                    )
                    session_rotated = session_rotated or bool(
                        turn_out.get("session_rotated")
                    )
                    state = store.get_or_create(effective_session_id, cid)
                else:
                    turn_out = run_local_controller_turn(text, state, bridge)

                s_dict = state.get("session_state") or {}
                session_ended = bool(
                    turn_out.get("session_ended")
                    or s_dict.get("call_complete") in ("true", "released")
                )
                if session_ended:
                    store.mark_session_ended(effective_session_id)

                elapsed_ms = int((time.time() - t0) * 1000)
                _, inspect_data = bridge.request(
                    "GET", f"/v1/dispute-workflow/{urllib.parse.quote(cid)}/inspect"
                )
                turn_out["user_text"] = text
                turn_out["latency_ms"] = elapsed_ms
                turn_out["session_id"] = effective_session_id
                turn_out["session_rotated"] = session_rotated
                turn_out["session_ended"] = session_ended
                turn_out["customer_id"] = cid
                store.record_turn(
                    cid,
                    {
                        "session_id": effective_session_id,
                        "user_text": text,
                        "agent_text": turn_out.get("agent_text", ""),
                        "active_agent": turn_out.get("active_agent", ""),
                        "agent_transitions": turn_out.get("agent_transitions", []),
                        "tool_calls": turn_out.get("tool_calls", []),
                        "api_exchanges": turn_out.get("api_exchanges", []),
                        "session_variables": turn_out.get("session_variables", {}),
                        "session_ended": session_ended,
                        "session_rotated": session_rotated,
                        "latency_ms": elapsed_ms,
                    },
                )
                inspect_data["recent_turns"] = store.get_recent_turns(cid)
                turn_out["workflow_inspect"] = inspect_data
                self._send_json(200, turn_out)
            except Exception:
                logger.exception("Session turn failed")
                self._send_json(500, {"error": "Internal server error processing session turn"})

        def _run_live_cxas_turn(
            self,
            effective_session_id: str,
            base_session_id: str,
            customer_id: str,
            text: str,
            state: dict[str, Any],
            is_first_turn: bool,
        ) -> dict[str, Any]:
            assert token_cache is not None
            creds = Credentials(
                token=token_cache.get_user_token(),
                quota_project_id=project_id,
            )
            sessions_client = Sessions(app_name=app_name, creds=creds)
            seed_vars = (
                {
                    "session_state": {
                        "customer_id": customer_id,
                        "conversation_ref": effective_session_id,
                    }
                }
                if is_first_turn
                else None
            )
            raw_resp = sessions_client.run(
                session_id=effective_session_id,
                text=text,
                variables=seed_vars,
            )
            parsed = sessions_client.get_structured_response(raw_resp)
            session_rotated = False

            # If the remote CXAS session was already closed (returns empty speech and
            # zero tool calls), automatically rotate to a fresh CXAS session ID and retry.
            if (
                not (parsed.get("agent_text") or "").strip()
                and not (parsed.get("tool_calls") or [])
            ):
                store.mark_session_ended(effective_session_id)
                effective_session_id = store.rotate_session_id(base_session_id)
                state = store.get_or_create(effective_session_id, customer_id)
                session_rotated = True
                raw_resp = sessions_client.run(
                    session_id=effective_session_id,
                    text=text,
                    variables={
                        "session_state": {
                            "customer_id": customer_id,
                            "conversation_ref": effective_session_id,
                        }
                    },
                )
                parsed = sessions_client.get_structured_response(raw_resp)

            updated_vars = extract_session_variables(raw_resp)
            s_dict = state.setdefault("session_state", {})
            updated_session_state = updated_vars.get("session_state")
            if isinstance(updated_session_state, dict):
                s_dict.update(updated_session_state)
            session_ended = bool(
                parsed.get("session_ended")
                or s_dict.get("call_complete") in ("true", "released")
            )

            tool_calls = [
                {
                    "name": tc["action"],
                    "args": dict(tc["args"]) if tc.get("args") else {},
                }
                for tc in (parsed.get("tool_calls") or [])
            ]
            tool_responses = [
                {
                    "name": str(tr["action"]).removeprefix("_response:"),
                    "response": dict(tr["response"]) if tr.get("response") else {},
                }
                for tr in (parsed.get("tool_responses") or [])
            ]
            transitions = ["authentication_agent"]
            if any(
                tc["name"] == "transfer_to_agent" or tc["name"] == "assist_card_dispute"
                for tc in tool_calls
            ) or s_dict.get("authenticated"):
                if "dispute_specialist_agent" not in transitions:
                    transitions.append("dispute_specialist_agent")
            active_agent = transitions[-1]
            live_exchanges = [
                {
                    "operation": tc["name"],
                    "request": tc["args"],
                    "response": (
                        tool_responses[idx]["response"]
                        if idx < len(tool_responses)
                        else {}
                    ),
                }
                for idx, tc in enumerate(tool_calls)
            ]
            return {
                "session_id": effective_session_id,
                "session_rotated": session_rotated,
                "session_ended": session_ended,
                "agent_text": (parsed.get("agent_text") or "").strip(),
                "active_agent": active_agent,
                "agent_transitions": transitions,
                "tool_calls": tool_calls,
                "tool_responses": tool_responses,
                "api_exchanges": live_exchanges,
                "session_variables": dict(s_dict),
            }

    return DemoUIHandler


def boot_embedded_temporal(
    asgi_loop: asyncio.AbstractEventLoop,
) -> tuple[Any, Any]:
    """Boot an in-process Temporal server + worker and return (env, asgi_client).

    Returning `env` alongside `asgi_client` ensures the WorkflowEnvironment handle
    stays strongly referenced for the lifetime of the server process.
    """
    from httpx import ASGITransport, AsyncClient
    from temporalio.contrib.pydantic import pydantic_data_converter
    from temporalio.testing import WorkflowEnvironment

    from temporal_service.app import app as temporal_app
    from temporal_service.app import runtime_manager

    async def _boot() -> tuple[Any, Any]:
        env = await WorkflowEnvironment.start_local(
            data_converter=pydantic_data_converter
        )
        await runtime_manager.start(existing_client=env.client)
        client = AsyncClient(
            transport=ASGITransport(app=temporal_app),
            base_url="http://embedded-temporal",
        )
        return env, client

    return asyncio.run_coroutine_threadsafe(_boot(), asgi_loop).result(
        timeout=30.0
    )


def main() -> None:
    """Start the Split-Screen Demo UI server (with optional embedded Temporal adapter)."""
    parser = argparse.ArgumentParser(description="GECX + Temporal.io Demo UI Server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8092")))
    parser.add_argument("--project-id", default=DEFAULT_PROJECT_ID)
    parser.add_argument("--cxas-location", default=DEFAULT_CXAS_LOCATION)
    parser.add_argument("--app-name", default=DEFAULT_APP_NAME)
    parser.add_argument("--adapter-url", default=DEFAULT_ADAPTER_URL)
    parser.add_argument("--account", default=DEFAULT_ACCOUNT)
    parser.add_argument(
        "--embedded-temporal",
        action="store_true",
        help="Start an in-process Temporal + FastAPI ASGI adapter automatically.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    asgi_loop = None
    asgi_client = None
    embedded_env = None
    if args.embedded_temporal:
        asgi_loop = asyncio.new_event_loop()
        t = threading.Thread(target=asgi_loop.run_forever, daemon=True)
        t.start()
        embedded_env, asgi_client = boot_embedded_temporal(asgi_loop)
        logger.info("Started embedded Temporal server + Worker in-process.")

    token_cache = TokenCache(account=args.account)
    bridge = AdapterBridge(
        adapter_url=args.adapter_url,
        token_cache=token_cache,
        asgi_loop=asgi_loop,
        asgi_client=asgi_client,
    )
    handler_cls = make_handler(
        bridge=bridge,
        token_cache=token_cache,
        project_id=args.project_id,
        cxas_location=args.cxas_location,
        app_name=args.app_name,
    )
    server = ThreadedHTTPServer((args.host, args.port), handler_cls)
    logger.info("Serving Split-Screen Demo UI on http://%s:%d", args.host, args.port)
    try:
        server.serve_forever()
    finally:
        _ = embedded_env


if __name__ == "__main__":
    main()
