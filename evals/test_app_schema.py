"""Static bundle, schema, and AST contract tests for cxas_app/gecx-temporal-workflow-demo."""

import ast
import inspect
import json
import re

from evals.conftest import (
    AFTER_MODEL_CB_PATH,
    APP_BUNDLE_DIR,
    AUTH_TOOL_PATH,
    BEFORE_MODEL_CB_PATH,
    DISPUTE_TOOL_PATH,
)

_FORBIDDEN_TRANSFER_WORDS = re.compile(
    r"\b(transfer|transferring|connecting|handoff)\b", re.IGNORECASE
)


def test_app_json_variable_declarations_and_top_level_rules() -> None:
    """Verify app.json declares required=[] on every variable and has no top-level tools."""
    app_data = json.loads((APP_BUNDLE_DIR / "app.json").read_text(encoding="utf-8"))
    assert "tools" not in app_data, "app.json must not declare top-level 'tools'"
    declarations = app_data.get("variableDeclarations", [])
    assert declarations, "variableDeclarations must not be empty"
    names = set()
    for decl in declarations:
        name = decl["name"]
        names.add(name)
        schema = decl.get("schema", {})
        assert "required" in schema and schema["required"] == [], (
            f"Variable '{name}' is missing 'required': [] in schema"
        )
    assert names == {"session_state"}, (
        "app.json must declare a single 'session_state' variable of type OBJECT"
    )
    session_schema = declarations[0]["schema"]
    assert session_schema["type"] == "OBJECT"
    assert session_schema.get("default") == {
        "customer_id": "customer-claire-bristol",
        "conversation_ref": "conv-default",
    }
    for py_path in (AUTH_TOOL_PATH, DISPUTE_TOOL_PATH):
        assert "_DEFAULT_CUSTOMER_ID" not in py_path.read_text(encoding="utf-8")


def test_child_agents_form_single_parent_acyclic_tree() -> None:
    """Verify childAgents across agents/*/*.json form a single-parent acyclic tree (S007)."""
    agents_dir = APP_BUNDLE_DIR / "agents"
    parents: dict[str, str] = {}
    for agent_dir in sorted(agents_dir.iterdir()):
        if not agent_dir.is_dir():
            continue
        config = json.loads(
            (agent_dir / f"{agent_dir.name}.json").read_text(encoding="utf-8")
        )
        for child in config.get("childAgents", []):
            assert child not in parents, f"Agent {child} has multiple parents"
            assert child != agent_dir.name, f"Agent {child} references itself"
            parents[child] = agent_dir.name
    assert parents == {"dispute_specialist_agent": "authentication_agent"}


def test_tool_json_descriptions_match_python_docstrings_and_signatures(
    load_controllers,
) -> None:
    """Verify tool JSON descriptions match Python docstrings verbatim and Args match signatures."""
    auth_fn, dispute_fn, _, _ = load_controllers()
    for tool_name, code_path, fn in (
        ("authenticate_caller", AUTH_TOOL_PATH, auth_fn),
        ("assist_card_dispute", DISPUTE_TOOL_PATH, dispute_fn),
    ):
        tool_json = json.loads(
            (APP_BUNDLE_DIR / "tools" / tool_name / f"{tool_name}.json").read_text(
                encoding="utf-8"
            )
        )
        json_desc = tool_json["pythonFunction"]["description"]
        tree = ast.parse(code_path.read_text(encoding="utf-8"))
        first_fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef))
        assert first_fn.name == tool_name, (
            f"First FunctionDef in {code_path} must be {tool_name} (T004)"
        )
        docstring = ast.get_docstring(first_fn)
        assert json_desc == docstring, f"Description mismatch in {tool_name}.json"

        params = set(inspect.signature(fn).parameters)
        args_block = docstring.split("Args:", 1)[1].split("\nReturns:", 1)[0]
        documented = set(re.findall(r"^ {4}(\w+):", args_block, flags=re.MULTILINE))
        assert documented == params, f"Docstring Args mismatch for {tool_name}"


def test_shared_callbacks_are_identical_and_obey_ast_hygiene() -> None:
    """Verify shared callbacks are byte-identical across agents (S005) and pass AST checks."""
    dispute_before = (
        APP_BUNDLE_DIR
        / "agents"
        / "dispute_specialist_agent"
        / "before_model_callbacks"
        / "guard_and_route"
        / "python_code.py"
    )
    dispute_after = (
        APP_BUNDLE_DIR
        / "agents"
        / "dispute_specialist_agent"
        / "after_model_callbacks"
        / "repair_and_transfer"
        / "python_code.py"
    )
    assert BEFORE_MODEL_CB_PATH.read_text(encoding="utf-8") == dispute_before.read_text(
        encoding="utf-8"
    )
    assert AFTER_MODEL_CB_PATH.read_text(encoding="utf-8") == dispute_after.read_text(
        encoding="utf-8"
    )

    for py_path in (
        AUTH_TOOL_PATH,
        DISPUTE_TOOL_PATH,
        BEFORE_MODEL_CB_PATH,
        AFTER_MODEL_CB_PATH,
    ):
        src = py_path.read_text(encoding="utf-8")
        assert "from __future__ import annotations" not in src, (
            f"C010 violation in {py_path}"
        )
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Delete):
                raise AssertionError(f"Forbidden del statement in {py_path}")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "pop"
            ):
                raise AssertionError(f"Forbidden .pop() call in {py_path}")


def test_instructions_have_zero_transfer_or_handoff_wording() -> None:
    """Verify no instruction.txt file contains transfer, transferring, connecting, or handoff."""
    for instr_path in [
        APP_BUNDLE_DIR / "global_instruction.txt",
        APP_BUNDLE_DIR / "agents" / "authentication_agent" / "instruction.txt",
        APP_BUNDLE_DIR / "agents" / "dispute_specialist_agent" / "instruction.txt",
    ]:
        text = instr_path.read_text(encoding="utf-8")
        assert not _FORBIDDEN_TRANSFER_WORDS.search(text), (
            f"Forbidden transfer/handoff wording found in {instr_path}"
        )
