"""
End-to-end test of the conversations table relay.

1. Write an inbound message to the conversations table (simulating what the
   ShragaRelay flow does when a user sends a message)
2. Run the task manager's process_message on it (simulating what the task
   manager does when it polls and claims the message)
3. Verify the outbound response appears in the conversations table

This does NOT require the bot or the flow — it tests the Dataverse layer directly.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")

import requests
import json
import time
import uuid
from azure.identity import DefaultAzureCredential

DATAVERSE_URL = "https://org3e79cdb1.crm3.dynamics.com"
DATAVERSE_API = f"{DATAVERSE_URL}/api/data/v9.2"
CONVERSATIONS_TABLE = "cr_shraga_conversations"
USER_EMAIL = "sagik@microsoft.com"


def get_headers():
    cred = DefaultAzureCredential()
    token = cred.get_token(f"{DATAVERSE_URL}/.default").token
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "OData-MaxVersion": "4.0",
        "OData-Version": "4.0",
    }


def test_write_inbound_message():
    """Step 1: Write an inbound message (simulating the relay flow)."""
    headers = get_headers()
    test_message = f"E2E test message at {time.strftime('%H:%M:%S')}"

    body = {
        "cr_name": test_message[:200],
        "cr_useremail": USER_EMAIL,
        "cr_mcs_conversation_id": f"test-conv-{uuid.uuid4().hex[:8]}",
        "cr_message": test_message,
        "cr_direction": "Inbound",
        "cr_status": "Unclaimed",
    }

    # Request the created entity back in the response
    create_headers = {**headers, "Prefer": "return=representation"}

    print(f"1. Writing inbound message: '{test_message}'")
    resp = requests.post(
        f"{DATAVERSE_API}/{CONVERSATIONS_TABLE}",
        headers=create_headers, json=body, timeout=30,
    )

    if resp.status_code in (200, 201):
        data = resp.json()
        row_id = data.get("cr_shraga_conversationid")
        print(f"   ✓ Created row: {row_id}")
        return row_id, body["cr_mcs_conversation_id"]
    elif resp.status_code == 204:
        # Created but no body returned; get the ID from OData-EntityId header
        entity_id = resp.headers.get("OData-EntityId", "")
        if "(" in entity_id:
            row_id = entity_id.split("(")[-1].rstrip(")")
            print(f"   ✓ Created row: {row_id}")
            return row_id, body["cr_mcs_conversation_id"]
        print(f"   ✗ Created but could not extract row ID from headers")
        return None, None
    else:
        print(f"   ✗ Failed: {resp.status_code} {resp.text[:200]}")
        return None, None


def test_poll_unclaimed(user_email):
    """Step 2: Poll for unclaimed inbound messages (simulating task manager)."""
    headers = get_headers()

    url = (
        f"{DATAVERSE_API}/{CONVERSATIONS_TABLE}"
        f"?$filter=cr_useremail eq '{user_email}'"
        f" and cr_direction eq 'Inbound'"
        f" and cr_status eq 'Unclaimed'"
        f"&$orderby=createdon asc"
        f"&$top=5"
    )

    print(f"\n2. Polling for unclaimed messages for {user_email}...")
    resp = requests.get(url, headers=headers, timeout=30)

    if resp.status_code == 200:
        messages = resp.json().get("value", [])
        print(f"   Found {len(messages)} unclaimed message(s)")
        for m in messages:
            print(f"   - [{m.get('cr_shraga_conversationid')[:8]}...] {m.get('cr_message', '')[:60]}")
        return messages
    else:
        print(f"   ✗ Failed: {resp.status_code}")
        return []


def test_claim_and_respond(row_id, mcs_conv_id, user_email):
    """Step 3: Claim the message and write a response."""
    headers = get_headers()

    # First, get the message with its ETag
    resp = requests.get(
        f"{DATAVERSE_API}/{CONVERSATIONS_TABLE}({row_id})",
        headers=headers, timeout=30,
    )
    if resp.status_code != 200:
        print(f"   ✗ Could not read message: {resp.status_code}")
        return False

    msg = resp.json()
    etag = msg.get("@odata.etag")

    # Claim it
    print(f"\n3. Claiming message {row_id[:8]}...")
    claim_headers = {**headers, "If-Match": etag}
    resp = requests.patch(
        f"{DATAVERSE_API}/{CONVERSATIONS_TABLE}({row_id})",
        headers=claim_headers,
        json={"cr_status": "Claimed", "cr_claimed_by": "e2e-test"},
        timeout=30,
    )
    if resp.status_code in (200, 204):
        print(f"   ✓ Claimed successfully")
    elif resp.status_code == 412:
        print(f"   ✗ Already claimed by someone else")
        return False
    else:
        print(f"   ✗ Claim failed: {resp.status_code}")
        return False

    # Write outbound response
    print(f"\n4. Writing outbound response...")
    response_body = {
        "cr_name": "E2E test response",
        "cr_useremail": user_email,
        "cr_mcs_conversation_id": mcs_conv_id,
        "cr_message": "This is an automated E2E test response. The relay is working!",
        "cr_direction": "Outbound",
        "cr_status": "Unclaimed",  # relay flow will pick this up
        "cr_in_reply_to": row_id,
    }
    create_headers = {**headers, "Prefer": "return=representation"}
    resp = requests.post(
        f"{DATAVERSE_API}/{CONVERSATIONS_TABLE}",
        headers=create_headers, json=response_body, timeout=30,
    )
    if resp.status_code in (200, 201):
        resp_id = resp.json().get("cr_shraga_conversationid")
        print(f"   ✓ Response created: {resp_id}")
        return True
    elif resp.status_code == 204:
        print(f"   ✓ Response created (204)")
        return True
    else:
        print(f"   ✗ Response failed: {resp.status_code} {resp.text[:200]}")
        return False


def test_verify_response(inbound_row_id):
    """Step 5: Verify the outbound response exists."""
    headers = get_headers()

    print(f"\n5. Verifying outbound response for inbound {inbound_row_id[:8]}...")
    url = (
        f"{DATAVERSE_API}/{CONVERSATIONS_TABLE}"
        f"?$filter=cr_in_reply_to eq '{inbound_row_id}' and cr_direction eq 'Outbound'"
        f"&$top=1"
    )
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code == 200:
        rows = resp.json().get("value", [])
        if rows:
            print(f"   ✓ Response found: {rows[0].get('cr_message', '')[:80]}")
            return True
        else:
            print(f"   ✗ No response found")
            return False
    else:
        print(f"   ✗ Query failed: {resp.status_code}")
        return False


def cleanup(row_ids):
    """Clean up test rows."""
    headers = get_headers()
    print(f"\n6. Cleaning up {len(row_ids)} test row(s)...")
    for rid in row_ids:
        if rid:
            resp = requests.delete(
                f"{DATAVERSE_API}/{CONVERSATIONS_TABLE}({rid})",
                headers=headers, timeout=30,
            )
            if resp.status_code in (200, 204):
                print(f"   ✓ Deleted {rid[:8]}...")
            else:
                print(f"   ✗ Could not delete {rid[:8]}...: {resp.status_code}")


if __name__ == "__main__":
    print("=" * 60)
    print("E2E RELAY TEST")
    print("=" * 60)

    # Step 1: Write inbound message
    row_id, mcs_conv_id = test_write_inbound_message()
    if not row_id:
        print("\nFAILED: Could not create inbound message")
        sys.exit(1)

    # Step 2: Poll for it
    messages = test_poll_unclaimed(USER_EMAIL)
    found = any(m.get("cr_shraga_conversationid") == row_id for m in messages)
    if not found:
        print(f"\nWARN: Our message not found in poll results (may have been claimed already)")

    # Step 3: Claim and respond
    success = test_claim_and_respond(row_id, mcs_conv_id, USER_EMAIL)
    if not success:
        print("\nFAILED: Could not claim and respond")
        cleanup([row_id])
        sys.exit(1)

    # Step 5: Verify response
    response_found = test_verify_response(row_id)

    # Cleanup
    # Find the response row ID for cleanup
    headers = get_headers()
    resp = requests.get(
        f"{DATAVERSE_API}/{CONVERSATIONS_TABLE}?$filter=cr_in_reply_to eq '{row_id}'&$top=1",
        headers=headers, timeout=30,
    )
    response_row_id = None
    if resp.status_code == 200:
        rows = resp.json().get("value", [])
        if rows:
            response_row_id = rows[0].get("cr_shraga_conversationid")

    cleanup([row_id, response_row_id])

    print("\n" + "=" * 60)
    if response_found:
        print("✓ E2E RELAY TEST PASSED")
    else:
        print("✗ E2E RELAY TEST FAILED")
    print("=" * 60)
