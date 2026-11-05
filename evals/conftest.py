"""Shared pytest fixtures for simulating the CX Agent Studio (GECX) Python runtime."""

import importlib.util
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_BUNDLE_DIR = REPO_ROOT / "cxas_app" / "gecx-temporal-workflow-demo"
AUTH_TOOL_PATH = (
    APP_BUNDLE_DIR
    / "tools"
    / "authenticate_caller"
    / "python_function"
    / "python_code.py"
)
DISPUTE_TOOL_PATH = (
    APP_BUNDLE_DIR
    / "tools"
    / "assist_card_dispute"
    / "python_function"
    / "python_code.py"
)
BEFORE_MODEL_CB_PATH = (
    APP_BUNDLE_DIR
    / "agents"
    / "authentication_agent"
    / "before_model_callbacks"
    / "guard_and_route"
    / "python_code.py"
)
AFTER_MODEL_CB_PATH = (
    APP_BUNDLE_DIR
    / "agents"
    / "authentication_agent"
    / "after_model_callbacks"
    / "repair_and_transfer"
    / "python_code.py"
)
FAKE_TOOLSET_CODE_PATH = (
    APP_BUNDLE_DIR
    / "toolsets"
    / "temporal_workflow_api"
    / "tool_fake_config"
    / "code_block"
    / "python_code.py"
)


def _load_fake_toolset_module() -> Any:
    """Import the canonical toolFakeConfig module from the CXAS app bundle."""
    spec = importlib.util.spec_from_file_location(
        "temporal_workflow_api_fake", FAKE_TOOLSET_CODE_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass
class FakeExternalResponse:
    """Mock of the CX Agent Studio ExternalResponse object."""

    status_code: int = 200
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def text(self) -> str:
        return json.dumps(self.payload) if self.payload else ""

    def json(self) -> dict[str, Any]:
        return self.payload


@dataclass
class _FakeToolDescriptor:
    name: str


class InMemoryTemporalToolset:
    """Mock of the runtime `tools` namespace backed by toolFakeConfig."""

    def __init__(self) -> None:
        self.fake_mod = _load_fake_toolset_module()
        self.fake_mod.reset_fake_workflows()
        self.verify_calls: list[dict[str, Any]] = []
        self.step_calls: list[dict[str, Any]] = []
        self.next_error_response: FakeExternalResponse | None = None

    def temporal_workflow_api_verify_caller(
        self, payload: dict[str, Any]
    ) -> FakeExternalResponse:
        self.verify_calls.append(dict(payload))
        if self.next_error_response is not None:
            resp = self.next_error_response
            self.next_error_response = None
            return resp
        result = self.fake_mod.fake_tool_call(
            _FakeToolDescriptor("temporal_workflow_api_verify_caller"),
            payload,
            None,
        )
        status = int(result.get("errorCode", 200)) if result.get("status") == "error" else 200
        return FakeExternalResponse(status_code=status, payload=result)

    def temporal_workflow_api_advance_dispute(
        self, payload: dict[str, Any]
    ) -> FakeExternalResponse:
        self.step_calls.append(dict(payload))
        if self.next_error_response is not None:
            resp = self.next_error_response
            self.next_error_response = None
            return resp
        result = self.fake_mod.fake_tool_call(
            _FakeToolDescriptor("temporal_workflow_api_advance_dispute"),
            payload,
            None,
        )
        status = int(result.get("errorCode", 200)) if result.get("status") == "error" else 200
        return FakeExternalResponse(status_code=status, payload=result)


def _load_app_default_state() -> dict[str, Any]:
    """Load default variable values declared in app.json variableDeclarations."""
    app_data = json.loads((APP_BUNDLE_DIR / "app.json").read_text(encoding="utf-8"))
    defaults: dict[str, Any] = {}
    for decl in app_data.get("variableDeclarations", []):
        name = decl.get("name")
        default_val = (decl.get("schema") or {}).get("default")
        if name and isinstance(default_val, dict):
            defaults[name] = dict(default_val)
    return defaults


@dataclass
class FakeRuntimeContext:
    """Mock of the CX Agent Studio `context` / `callback_context` object."""

    state: dict[str, Any] = field(default_factory=dict)
    agent_name: str = "projects/test/locations/eu/apps/app/agents/authentication_agent"
    session_id: str = "session-live-001"
    invocation_id: str = "inv-001"

    def __post_init__(self) -> None:
        for var_name, default_val in _load_app_default_state().items():
            if var_name not in self.state:
                self.state[var_name] = dict(default_val)

    @property
    def variables(self) -> dict[str, Any]:
        return self.state


@dataclass
class FakeFunctionCall:
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    id: str | None = None


@dataclass
class FakeFunctionResponse:
    name: str
    response: dict[str, Any] = field(default_factory=dict)


@dataclass
class FakePart:
    text: str | None = None
    function_call: FakeFunctionCall | None = None
    function_response: FakeFunctionResponse | None = None
    agent_transfer: str | None = None
    end_session: dict[str, Any] | None = None

    @classmethod
    def from_text(cls, *, text: str) -> "FakePart":
        return cls(text=text)

    @classmethod
    def from_function_call(cls, *, name: str, args: dict[str, Any]) -> "FakePart":
        return cls(function_call=FakeFunctionCall(name=name, args=dict(args)))

    @classmethod
    def from_agent_transfer(cls, *, agent: str) -> "FakePart":
        return cls(agent_transfer=agent)

    @classmethod
    def from_end_session(cls, *, reason: str = "", escalated: bool = False) -> "FakePart":
        return cls(end_session={"reason": reason, "escalated": escalated})


@dataclass
class FakeContent:
    parts: list[FakePart] = field(default_factory=list)


@dataclass
class FakeLlmConfig:
    hidden_tools: list[str] = field(default_factory=list)

    def hide_tool(self, name: str) -> None:
        if name not in self.hidden_tools:
            self.hidden_tools.append(name)


@dataclass
class FakeLlmRequest:
    contents: list[FakeContent] = field(default_factory=list)
    config: FakeLlmConfig = field(default_factory=FakeLlmConfig)


@dataclass
class FakeLlmResponse:
    content: FakeContent = field(default_factory=FakeContent)

    @classmethod
    def from_parts(cls, *, parts: list[FakePart]) -> "FakeLlmResponse":
        return cls(content=FakeContent(parts=list(parts)))


@pytest.fixture
def load_controllers():
    """Factory that loads authenticate_caller and assist_card_dispute with shared state/tools."""
    auth_code = AUTH_TOOL_PATH.read_text(encoding="utf-8")
    dispute_code = DISPUTE_TOOL_PATH.read_text(encoding="utf-8")

    def _loader(
        initial_state: dict[str, Any] | None = None,
    ) -> tuple[Any, Any, FakeRuntimeContext, InMemoryTemporalToolset]:
        ctx = FakeRuntimeContext(state=dict(initial_state or {}))
        tools_mock = InMemoryTemporalToolset()
        auth_ns: dict[str, Any] = {"context": ctx, "tools": tools_mock}
        dispute_ns: dict[str, Any] = {"context": ctx, "tools": tools_mock}
        exec(compile(auth_code, str(AUTH_TOOL_PATH), "exec"), auth_ns)
        exec(compile(dispute_code, str(DISPUTE_TOOL_PATH), "exec"), dispute_ns)
        return (
            auth_ns["authenticate_caller"],
            dispute_ns["assist_card_dispute"],
            ctx,
            tools_mock,
        )

    return _loader


@pytest.fixture
def load_callbacks():
    """Factory that loads before_model_callback and after_model_callback."""
    before_code = BEFORE_MODEL_CB_PATH.read_text(encoding="utf-8")
    after_code = AFTER_MODEL_CB_PATH.read_text(encoding="utf-8")

    def _loader(
        initial_state: dict[str, Any] | None = None,
        agent_name: str = "authentication_agent",
        session_id: str = "session-live-001",
    ) -> tuple[Any, Any, FakeRuntimeContext]:
        ctx = FakeRuntimeContext(
            state=dict(initial_state or {}),
            agent_name=f"projects/test/locations/eu/apps/app/agents/{agent_name}",
            session_id=session_id,
        )
        ns: dict[str, Any] = {
            "CallbackContext": FakeRuntimeContext,
            "LlmRequest": FakeLlmRequest,
            "LlmResponse": FakeLlmResponse,
            "Part": FakePart,
            "Content": FakeContent,
        }
        before_ns = dict(ns)
        after_ns = dict(ns)
        exec(compile(before_code, str(BEFORE_MODEL_CB_PATH), "exec"), before_ns)
        exec(compile(after_code, str(AFTER_MODEL_CB_PATH), "exec"), after_ns)
        return (
            before_ns["before_model_callback"],
            after_ns["after_model_callback"],
            ctx,
        )

    return _loader
