#!/usr/bin/env python3
"""
Update a Power Automate flow definition via the Power Automate API.

Reads a flow JSON export, extracts the definition and connection references,
then PATCHes the flow via api.flow.microsoft.com.

Usage:
    python scripts/update_flow.py --flow-id <guid> --json-file flow.json
    python scripts/update_flow.py --flow-id <guid> --json-file flow.json --dry-run

Known flow IDs:
    TaskProgressUpdater: 4075d69d-eef6-4a67-81c3-2ea8cc49c5b5
    TaskCompleted:       da211a8a-3ef5-4291-bd91-67c4e6e88aec
    TaskFailed:          a4b59d39-a30f-4f4b-a07f-23b5a513bd11
    TaskRunner:          ae21fda1-a415-4e88-8cd4-c90fb0321faf
    TaskCanceled:        6db87fc1-a341-4b5d-843b-dfcc68054194
    SendMessage:         f6144661-8f48-9528-f120-b1666abccea0
"""
import argparse
import json
import os
import sys
import requests
from azure.identity import AzureCliCredential

ENV_ID = os.environ.get("PA_ENVIRONMENT_ID", "8660590d-33ac-ecd8-aef3-e4dd0d6071f4")
PA_API = "https://api.flow.microsoft.com/providers/Microsoft.ProcessSimple"
REQUEST_TIMEOUT = 30


def get_token() -> str:
    """Get token for Power Automate API. Tries AzureCliCredential first, then DefaultAzureCredential."""
    from azure.identity import DefaultAzureCredential
    for cred_cls in [AzureCliCredential, DefaultAzureCredential]:
        try:
            cred = cred_cls()
            token = cred.get_token("https://service.flow.microsoft.com/.default")
            return token.token
        except Exception:
            continue
    raise RuntimeError("Could not acquire token. Run 'az login' first.")


def load_flow_json(json_file: str) -> dict:
    """Load and parse the flow JSON file."""
    with open(json_file, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(
        description="Update a Power Automate flow definition via the PA API.",
    )
    parser.add_argument(
        "--flow-id",
        required=True,
        help="Flow GUID (e.g., '4075d69d-eef6-4a67-81c3-2ea8cc49c5b5').",
    )
    parser.add_argument(
        "--json-file",
        required=True,
        help="Path to the exported flow JSON file.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without making changes.",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("Power Automate Flow - Definition Update")
    print("=" * 60)
    print(f"  Environment: {ENV_ID}")
    print(f"  Flow ID:     {args.flow_id}")
    print(f"  JSON file:   {args.json_file}")
    print(f"  Dry run:     {args.dry_run}")
    print("=" * 60)
    print()

    # --- Load flow JSON ---
    print(f"[FILE] Loading flow JSON from {args.json_file}...")
    try:
        flow_json = load_flow_json(args.json_file)
    except FileNotFoundError:
        print(f"[ERROR] File not found: {args.json_file}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"[ERROR] Invalid JSON in {args.json_file}: {e}")
        sys.exit(1)

    definition = flow_json.get("properties", {}).get("definition")
    conn_refs = flow_json.get("properties", {}).get("connectionReferences", {})

    if not definition:
        print("[ERROR] Could not find 'properties.definition' in the JSON file.")
        sys.exit(1)

    print(f"[FILE] Definition actions: {len(definition.get('actions', {}))}")
    print(f"[FILE] Connection refs: {len(conn_refs)}")
    print()

    # --- Authenticate ---
    print("[AUTH] Acquiring token for Power Automate API...")
    try:
        token = get_token()
    except Exception as e:
        print(f"[ERROR] Failed to get token: {e}")
        print("Make sure you are logged in (az login).")
        sys.exit(1)
    print("[AUTH] Token acquired.")
    print()

    # --- Verify flow exists ---
    flow_url = f"{PA_API}/environments/{ENV_ID}/flows/{args.flow_id}?api-version=2016-11-01"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    print(f"[GET] Verifying flow {args.flow_id}...")
    resp = requests.get(flow_url, headers=headers, timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        print(f"[ERROR] Flow not found or inaccessible: {resp.status_code}")
        print(f"  {resp.text[:300]}")
        sys.exit(1)

    current = resp.json()
    flow_name = current.get("properties", {}).get("displayName", "?")
    flow_state = current.get("properties", {}).get("state", "?")
    print(f"[GET] Found: {flow_name} (state: {flow_state})")
    print()

    # --- Dry run ---
    if args.dry_run:
        print("[DRY RUN] Would PATCH the flow with updated definition and connectionReferences.")
        print(f"  Flow: {flow_name} ({args.flow_id})")
        print(f"  Actions: {len(definition.get('actions', {}))}")
        print(f"  Connections: {len(conn_refs)}")
        print()
        print("[DRY RUN] No changes made.")
        sys.exit(0)

    # --- PATCH flow definition ---
    print(f"[PATCH] Updating {flow_name}...")
    body = {"properties": {"definition": definition, "connectionReferences": conn_refs}}
    resp = requests.patch(flow_url, headers=headers, json=body, timeout=REQUEST_TIMEOUT)

    if resp.status_code == 200:
        print("[OK] Flow definition updated successfully!")
    else:
        print(f"[ERROR] PATCH failed: {resp.status_code}")
        print(f"  {resp.text[:500]}")
        sys.exit(1)

    print()
    print("=" * 60)
    print(f"  Flow:   {flow_name}")
    print(f"  Status: Definition updated")
    print("=" * 60)


if __name__ == "__main__":
    main()
