"""
DEPRECATED -- Device code auth integration test.

This test script exercised the device-code OAuth flow which is now blocked by
Azure Conditional Access policies.  See orchestrator_auth_devicecode.py for
details.  All functions below are skipped; the file is retained for reference.
"""
import os
import sys
import json
import pytest
import requests
from azure.identity import DeviceCodeCredential

pytestmark = pytest.mark.skip(
    reason="Device code auth is blocked by Conditional Access; dead code removed (GAP-M10)"
)

TENANT_ID = "72f988bf-86f1-41af-91ab-2d7cd011db47"
DATAVERSE_URL = "https://org3e79cdb1.crm3.dynamics.com"
CONVERSATIONS_TABLE = "cr_shraga_conversations"

# The user email and conv ID to send the device code link to via DV
# (simulating what the parent GM would do)
TARGET_USER = os.environ.get("TARGET_USER", "testuser@contoso.com")
MCS_CONV_ID = os.environ.get("MCS_CONV_ID", "")

def device_code_callback(verification_uri, user_code, expires_on):
    """Called when device code is ready — we relay this to the user via DV."""
    print(f"\n{'='*60}")
    print(f"[DEVICE CODE AUTH REQUIRED]")
    print(f"URL:  {verification_uri}")
    print(f"Code: {user_code}")
    print(f"Expires: {expires_on}")
    print(f"{'='*60}\n")

    # Write to DV so the user sees it in Teams
    # Use a SEPARATE credential (the parent GM's az login) to write to DV
    from azure.identity import DefaultAzureCredential
    try:
        parent_cred = DefaultAzureCredential()
        parent_token = parent_cred.get_token(f"{DATAVERSE_URL}/.default")

        headers = {
            "Authorization": f"Bearer {parent_token.token}",
            "Content-Type": "application/json",
            "OData-Version": "4.0",
        }

        message = (
            f"Please authenticate the dedicated session for your dev box.\n\n"
            f"Open this URL: {verification_uri}\n"
            f"Enter code: **{user_code}**\n\n"
            f"This is a one-time setup. After you authenticate, your personal worker will be ready."
        )

        # Find the latest inbound message from the user to reply to
        search_url = (
            f"{DATAVERSE_URL}/api/data/v9.2/{CONVERSATIONS_TABLE}"
            f"?$filter=cr_useremail eq '{TARGET_USER}'"
            f" and cr_direction eq 'Inbound'"
            f"&$orderby=createdon desc&$top=1"
        )
        resp = requests.get(search_url, headers={"Authorization": f"Bearer {parent_token.token}", "Accept": "application/json"}, timeout=10)
        rows = resp.json().get("value", [])

        if rows:
            reply_to = rows[0]["cr_shraga_conversationid"]
            conv_id = rows[0].get("cr_mcs_conversation_id", MCS_CONV_ID)
        else:
            reply_to = ""
            conv_id = MCS_CONV_ID

        body = {
            "cr_name": message[:200],
            "cr_useremail": TARGET_USER,
            "cr_mcs_conversation_id": conv_id,
            "cr_message": message,
            "cr_direction": "Outbound",
            "cr_status": "Unclaimed",
            "cr_in_reply_to": reply_to,
        }

        resp = requests.post(
            f"{DATAVERSE_URL}/api/data/v9.2/{CONVERSATIONS_TABLE}",
            headers=headers, json=body, timeout=10
        )
        if resp.status_code in (200, 201, 204):
            print(f"[RELAY] Device code sent to user via DV/Teams (reply_to={reply_to[:8]})")
        else:
            print(f"[RELAY] Failed to send to DV: {resp.status_code}")
    except Exception as e:
        print(f"[RELAY] Could not relay via DV: {e}")
        print(f"[RELAY] User must manually open {verification_uri} and enter {user_code}")


def main():
    print("[START] Dedicated GM session (no az login)")
    print("[AUTH] Initiating device code authentication...")

    # Force device code — this simulates a machine with no az login
    credential = DeviceCodeCredential(
        tenant_id=TENANT_ID,
        prompt_callback=lambda uri, code, expires: device_code_callback(uri, code, expires),
    )

    # Try to get a Dataverse token — this triggers the device code flow
    print("[AUTH] Requesting Dataverse token (will trigger device code)...")
    token = credential.get_token(f"{DATAVERSE_URL}/.default")
    print(f"[AUTH] Authenticated successfully! Token expires at {token.expires_on}")

    # Prove we can access DV
    print("[TEST] Querying DV to prove access...")
    headers = {
        "Authorization": f"Bearer {token.token}",
        "Accept": "application/json",
    }
    resp = requests.get(
        f"{DATAVERSE_URL}/api/data/v9.2/{CONVERSATIONS_TABLE}?$top=1",
        headers=headers, timeout=10
    )
    if resp.status_code == 200:
        count = len(resp.json().get("value", []))
        print(f"[TEST] DV access confirmed! Got {count} row(s)")
    else:
        print(f"[TEST] DV access failed: {resp.status_code}")

    print("[DONE] Dedicated session is authenticated and operational.")


if __name__ == "__main__":
    main()
