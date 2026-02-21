#!/usr/bin/env python3
"""
Standalone script to check Dev Box status via the Azure DevCenter API.

Extracted from GlobalManager._tool_check_devbox_status (global_manager.py).

Usage:
    # Check a specific dev box by name (requires --user as well):
    python scripts/check_devbox_status.py --name shraga-box-01 --user <azure-ad-id>

    # List all dev boxes for a user:
    python scripts/check_devbox_status.py --user <azure-ad-id>

Environment variables (required):
    DEVCENTER_ENDPOINT  - DevCenter API endpoint URL
    DEVBOX_PROJECT      - DevCenter project name

Environment variables (optional):
    DEVBOX_POOL         - DevCenter pool name (default: botdesigner-pool-italynorth)

Returns JSON on stdout.  Exit codes:
    0 - success
    1 - error (not-found, misconfiguration, API failure)
"""

import argparse
import json
import os
import sys
from pathlib import Path

# Add the repository root to sys.path so we can import orchestrator_devbox
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from orchestrator_devbox import DevBoxManager, DevBoxInfo


def _build_manager() -> DevBoxManager:
    """Construct a DevBoxManager from environment variables.

    Raises SystemExit with a descriptive JSON error when required
    environment variables are missing.
    """
    endpoint = os.environ.get("DEVCENTER_ENDPOINT")
    project = os.environ.get("DEVBOX_PROJECT")
    pool = os.environ.get("DEVBOX_POOL", "botdesigner-pool-italynorth")

    missing = []
    if not endpoint:
        missing.append("DEVCENTER_ENDPOINT")
    if not project:
        missing.append("DEVBOX_PROJECT")

    if missing:
        _exit_error(
            f"Missing required environment variable(s): {', '.join(missing)}",
            exit_code=1,
        )

    return DevBoxManager(
        devcenter_endpoint=endpoint,
        project_name=project,
        pool_name=pool,
    )


def _exit_error(message: str, exit_code: int = 1) -> None:
    """Print a JSON error object to stdout and exit."""
    print(json.dumps({"error": message}, indent=2))
    sys.exit(exit_code)


def check_single_devbox(manager: DevBoxManager, user_id: str, name: str) -> dict:
    """Query status for a single named dev box.

    Returns a dict with provisioning_state, status, connection_url, and name.
    On not-found or API error, returns a dict with an 'error' key.
    """
    try:
        info: DevBoxInfo = manager.get_devbox_status(user_id, name)
        return {
            "name": info.name,
            "provisioning_state": info.provisioning_state,
            "status": info.status,
            "connection_url": info.connection_url,
        }
    except Exception as exc:
        error_msg = str(exc)
        # Detect 404 / not-found from the DevCenter API
        if "404" in error_msg or "not found" in error_msg.lower():
            return {"error": f"Dev box '{name}' not found for user '{user_id}'."}
        return {"error": error_msg}


def list_user_devboxes(manager: DevBoxManager, user_id: str) -> dict:
    """List all dev boxes for a given user.

    Returns a dict with a 'devboxes' list on success, or an 'error' key on failure.
    """
    try:
        boxes = manager.list_devboxes(user_id)
        result_list = []
        for box in boxes:
            result_list.append({
                "name": box.get("name", ""),
                "provisioning_state": box.get("provisioningState", "Unknown"),
                "status": box.get("powerState", "Unknown"),
            })
        return {"devboxes": result_list, "count": len(result_list)}
    except Exception as exc:
        error_msg = str(exc)
        if "404" in error_msg or "not found" in error_msg.lower():
            return {"error": f"No dev boxes found for user '{user_id}'."}
        return {"error": error_msg}


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Check Dev Box status via the Azure DevCenter API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  # Check a specific box:\n"
            "  python scripts/check_devbox_status.py --name shraga-box-01 --user <azure-ad-id>\n"
            "\n"
            "  # List all boxes for a user:\n"
            "  python scripts/check_devbox_status.py --user <azure-ad-id>\n"
        ),
    )
    parser.add_argument(
        "--name",
        type=str,
        default=None,
        help="Name of the dev box to check (e.g. shraga-box-01).",
    )
    parser.add_argument(
        "--user",
        type=str,
        required=True,
        help="Azure AD object ID (GUID) of the dev box owner.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns exit code (0 = success, 1 = error)."""
    parser = build_parser()
    args = parser.parse_args(argv)

    manager = _build_manager()

    if args.name:
        result = check_single_devbox(manager, args.user, args.name)
    else:
        result = list_user_devboxes(manager, args.user)

    print(json.dumps(result, indent=2))

    if "error" in result:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
