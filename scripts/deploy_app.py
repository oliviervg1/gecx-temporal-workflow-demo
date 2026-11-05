#!/usr/bin/env python3
"""Deploys the CXAS bundle to CX Agent Studio (GECX) and verifies Gate 3 registration.

Runs static bundle linting, verifies that environment.json points to a real
Cloud Run adapter URL (not the placeholder), pushes cxas_app/gecx-temporal-workflow-demo
to the target GCP project (location eu) via cxas-scrapi, verifies that all custom Python
controller tools and OpenAPI toolsets are registered on the platform (Gate 3:
Platform tools (N) > 0), re-pushes evaluation suites after an overwrite push,
and snapshots an immutable AppVersion.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from cxas_scrapi.cli.app import _app_push
from cxas_scrapi.core.agents import Agents
from cxas_scrapi.core.apps import Apps
from cxas_scrapi.core.tools import Tools
from cxas_scrapi.core.versions import Versions
from google.oauth2.credentials import Credentials

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_APP_DIR = REPO_ROOT / "cxas_app" / "gecx-temporal-workflow-demo"
DEFAULT_PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
DEFAULT_LOCATION = os.environ.get("CXAS_LOCATION", "eu")
DEFAULT_ACCOUNT = os.environ.get("GCLOUD_ACCOUNT", "")
EVAL_FILES = (REPO_ROOT / "evals" / "goldens" / "goldens.yaml",)


def get_credentials(account: str, project_id: str) -> Credentials:
    """Builds OAuth2 Credentials from gcloud for the target account and project.

    Args:
        account: Optional active gcloud user account email.
        project_id: GCP project ID used as quota project.

    Returns:
        Configured google.oauth2.credentials.Credentials instance.
    """
    cmd = ["gcloud", "auth", "print-access-token"]
    if account:
        cmd.append(f"--account={account}")
    token = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()
    return Credentials(token=token, quota_project_id=project_id)


def resolve_bundle_dir(app_dir: Path) -> Path:
    """Finds the directory containing app.json under app_dir."""
    if (app_dir / "app.json").exists():
        return app_dir
    for child in sorted(app_dir.iterdir()):
        if child.is_dir() and (child / "app.json").exists():
            return child
    raise FileNotFoundError(f"Could not find app.json under {app_dir}")


def verify_environment_url(bundle_dir: Path, allow_placeholder: bool = False) -> str:
    """Verifies that environment.json specifies a real Cloud Run URL before push."""
    env_path = bundle_dir / "environment.json"
    env_data = json.loads(env_path.read_text(encoding="utf-8"))
    url = (
        env_data.get("toolsets", {})
        .get("temporal_workflow_api", {})
        .get("openApiToolset", {})
        .get("url", "")
    )
    if not url or ("example.run.app" in url and not allow_placeholder):
        raise RuntimeError(
            f"environment.json still contains placeholder URL '{url}'. "
            "Deploy the Temporal adapter first (./scripts/deploy_adapter.sh) "
            "or pass --adapter-url <URL>."
        )
    return url


def run_lint_check(bundle_dir: Path) -> None:
    """Runs cxas lint from the repository root when cxaslint.yaml is present."""
    lint_cwd = (
        REPO_ROOT
        if bundle_dir.resolve() == DEFAULT_APP_DIR.resolve()
        and (REPO_ROOT / "cxaslint.yaml").exists()
        else bundle_dir
    )
    print(f"=== Gate 1: Running cxas lint in {lint_cwd} ===")
    cxas_bin = Path(sys.executable).parent / "cxas"
    cmd = [str(cxas_bin) if cxas_bin.exists() else "cxas", "lint"]
    subprocess.run(cmd, cwd=str(lint_cwd), check=True)


def verify_gate3_platform_tools(
    app_name: str,
    bundle_dir: Path,
    creds: Credentials,
) -> dict[str, Any]:
    """Verifies Gate 3: Platform tools (N) > 0 and local/remote tool parity."""
    print(f"\n=== Gate 3: Verifying Platform Tool & Agent Registration ({app_name}) ===")
    parts = app_name.split("/")
    project_id, location = parts[1], parts[3]

    apps_client = Apps(project_id=project_id, location=location, creds=creds)
    remote_app = apps_client.get_app(app_name=app_name)

    agents_client = Agents(app_name=app_name, creds=creds)
    agents_map = agents_client.get_agents_map(reverse=True)

    local_tools = (
        sorted(d.name for d in (bundle_dir / "tools").iterdir() if d.is_dir())
        if (bundle_dir / "tools").exists()
        else []
    )
    local_toolsets = (
        sorted(d.name for d in (bundle_dir / "toolsets").iterdir() if d.is_dir())
        if (bundle_dir / "toolsets").exists()
        else []
    )

    tools_client = Tools(app_name=app_name, creds=creds)
    remote_custom_tools = list(tools_client.client.list_tools(parent=app_name))
    for stale_tool in remote_custom_tools:
        if (
            stale_tool.display_name not in local_tools
            and stale_tool.display_name != "end_session"
        ):
            print(
                f"  Deleting stale remote tool '{stale_tool.display_name}' "
                f"({stale_tool.name})..."
            )
            tools_client.delete_tool(tool_name=stale_tool.name)

    remote_tools = tools_client.list_tools()
    tools_map = {t.display_name: t.name for t in remote_tools}

    remote_toolsets = list(tools_client.client.list_toolsets(parent=app_name))
    toolsets_map = {ts.display_name: ts.name for ts in remote_toolsets}

    print(f"  Platform tools ({len(tools_map)}):")
    for t_name in sorted(tools_map):
        print(f"    - {t_name}")

    print(f"  Platform OpenAPI toolsets ({len(toolsets_map)}):")
    for ts_name in sorted(toolsets_map):
        print(f"    - {ts_name}")

    missing_tools = [t for t in local_tools if t not in tools_map]
    missing_toolsets = [ts for ts in local_toolsets if ts not in toolsets_map]

    if not tools_map:
        raise RuntimeError(
            "Gate 3 FAILED: Platform tools (0): CXAS import_app dropped all custom tools! "
            "Check return type annotations and Google-style docstrings in python_code.py."
        )
    if missing_tools or missing_toolsets:
        raise RuntimeError(
            f"Gate 3 FAILED: Missing remote tools={missing_tools}, "
            f"missing remote toolsets={missing_toolsets}"
        )
    if not remote_app.root_agent:
        raise RuntimeError("Gate 3 FAILED: Remote app has no root_agent configured.")

    print("  Gate 3 PASSED: All controller tools, OpenAPI toolsets, and root_agent verified.")
    return {
        "app_name": app_name,
        "display_name": remote_app.display_name,
        "platform_tools_count": len(tools_map),
        "platform_tools": sorted(tools_map.keys()),
        "platform_toolsets_count": len(toolsets_map),
        "platform_toolsets": sorted(toolsets_map.keys()),
        "agents": sorted(agents_map.keys()),
    }


def push_evaluations(app_name: str, delay_seconds: int) -> list[str]:
    """Re-pushes golden evaluations after an app overwrite."""
    existing_files = [path for path in EVAL_FILES if path.exists()]
    if not existing_files:
        return []

    if delay_seconds > 0:
        print(
            f"\n=== Waiting {delay_seconds}s for import_app to settle before push-eval ==="
        )
        time.sleep(delay_seconds)

    cxas_bin = Path(sys.executable).parent / "cxas"
    cxas_cmd = str(cxas_bin) if cxas_bin.exists() else "cxas"
    pushed: list[str] = []
    for eval_path in existing_files:
        rel_path = str(eval_path.relative_to(REPO_ROOT))
        print(f"  Pushing evaluation file: {rel_path}")
        subprocess.run(
            [cxas_cmd, "push-eval", "--app-name", app_name, "--file", str(eval_path)],
            cwd=str(REPO_ROOT),
            check=True,
        )
        pushed.append(rel_path)
    return pushed


def main() -> int:
    """CLI entrypoint for deploying the CXAS app bundle and running Gate 3."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-dir", type=Path, default=DEFAULT_APP_DIR)
    parser.add_argument("--project-id", default=DEFAULT_PROJECT_ID)
    parser.add_argument("--location", default=DEFAULT_LOCATION)
    parser.add_argument("--account", default=DEFAULT_ACCOUNT)
    parser.add_argument("--app-name", default="", help="Optional existing app resource name.")
    parser.add_argument(
        "--adapter-url",
        default="",
        help="Optional Cloud Run adapter URL to write into environment.json before push.",
    )
    parser.add_argument("--allow-placeholder-url", action="store_true")
    parser.add_argument("--skip-lint", action="store_true", help="Skip local cxas lint.")
    parser.add_argument(
        "--skip-evals",
        action="store_true",
        help="Skip re-pushing evaluations after app push.",
    )
    parser.add_argument(
        "--eval-delay-seconds",
        type=int,
        default=15,
        help="Seconds to wait after overwrite before pushing evaluations.",
    )
    parser.add_argument(
        "--no-version",
        action="store_true",
        help="Do not cut an immutable AppVersion snapshot after push.",
    )
    args = parser.parse_args()
    if not args.project_id:
        raise RuntimeError(
            "GCP project ID is required. Set GOOGLE_CLOUD_PROJECT or pass --project-id."
        )

    bundle_dir = resolve_bundle_dir(args.app_dir)
    if args.adapter_url:
        env_path = bundle_dir / "environment.json"
        env_path.write_text(
            json.dumps(
                {
                    "toolsets": {
                        "temporal_workflow_api": {
                            "openApiToolset": {"url": args.adapter_url.rstrip("/")}
                        }
                    }
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    adapter_url = verify_environment_url(bundle_dir, args.allow_placeholder_url)
    print(f"Verified environment.json adapter URL: {adapter_url}")

    app_manifest = json.loads((bundle_dir / "app.json").read_text(encoding="utf-8"))
    display_name = app_manifest["displayName"]

    if not args.skip_lint:
        run_lint_check(bundle_dir)

    creds = get_credentials(args.account, args.project_id)
    apps_client = Apps(project_id=args.project_id, location=args.location, creds=creds)

    target_app_name = args.app_name
    if not target_app_name:
        existing_app = apps_client.get_app_by_display_name(display_name)
        if existing_app is not None:
            target_app_name = existing_app.name
            print(f"Found existing CXAS app '{display_name}': {target_app_name}")
        else:
            print(f"No existing CXAS app named '{display_name}'; creating new app...")

    push_ns = argparse.Namespace(
        overwrite=bool(target_app_name),
        create_version=False,
    )
    deployed_app_name = _app_push(
        app_dir=str(bundle_dir),
        apps_client=apps_client,
        target_app_name=target_app_name or None,
        identifier=target_app_name or None,
        display_name=display_name,
        args=push_ns,
    )
    if not deployed_app_name:
        raise RuntimeError("Deployment failed to return an app resource name.")

    gate3_summary = verify_gate3_platform_tools(
        app_name=deployed_app_name,
        bundle_dir=bundle_dir,
        creds=creds,
    )
    gate3_summary["adapter_url"] = adapter_url

    pushed_evals: list[str] = []
    if not args.skip_evals:
        pushed_evals = push_evaluations(
            app_name=deployed_app_name,
            delay_seconds=args.eval_delay_seconds if push_ns.overwrite else 5,
        )
    gate3_summary["pushed_evaluations"] = pushed_evals

    version_name = ""
    if not args.no_version:
        versions_client = Versions(app_name=deployed_app_name, creds=creds)
        ver_display = f"v-{time.strftime('%Y%m%d-%H%M%S')}"
        version = versions_client.create_version(
            display_name=ver_display,
            description="Automated release snapshot from scripts/deploy_app.py",
        )
        version_name = version.name
        print(f"  Created immutable AppVersion snapshot: {version_name} ({ver_display})")

    gate3_summary["version_name"] = version_name
    print("\nDeployment Summary:")
    print(json.dumps(gate3_summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
