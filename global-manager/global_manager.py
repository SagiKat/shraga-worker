"""
Global Manager -- Agentic architecture.

Polls the conversations table for any unclaimed inbound messages older than 15s.
Handles new user onboarding (dev box provisioning) and users whose personal
manager is down.

Instead of a hardcoded state-machine pipeline, the GM provides Claude with
tools (provision_devbox, check_devbox_status, apply_customizations, etc.) and
a system prompt describing guidelines.  Claude decides what to do based on
the user's message, their current state, and available tools.

The ONLY hardcoded user-facing message is the single fallback for when Claude
is completely unavailable:
    "The system is temporarily unavailable, please try again shortly."
"""
import requests
import json
import time
import os
import sys
import subprocess
import uuid
from pathlib import Path
from datetime import datetime, timezone, timedelta
from azure.identity import DefaultAzureCredential, DeviceCodeCredential
from azure.core.credentials import AccessToken

# Unique instance ID for this process (helps distinguish multiple GM instances)
INSTANCE_ID = uuid.uuid4().hex[:8]

sys.path.insert(0, str(Path(__file__).parent.parent))
from orchestrator_devbox import DevBoxManager, DevBoxInfo
from claude_auth_teams import ClaudeAuthManager, TeamsClaudeAuth, RemoteDevBoxAuth


DATAVERSE_URL = os.environ.get("DATAVERSE_URL", "https://org3e79cdb1.crm3.dynamics.com")
DATAVERSE_API = f"{DATAVERSE_URL}/api/data/v9.2"
CONVERSATIONS_TABLE = os.environ.get("CONVERSATIONS_TABLE", "cr_shraga_conversations")
TASKS_TABLE = os.environ.get("TASKS_TABLE", "cr_shraga_tasks")
USERS_TABLE = os.environ.get("USERS_TABLE", "crb3b_shragausers")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "5"))  # seconds
CLAIM_DELAY_NEW_USER = int(os.environ.get("CLAIM_DELAY_NEW_USER", "0"))  # immediate for new users
CLAIM_DELAY_KNOWN_USER = int(os.environ.get("CLAIM_DELAY_KNOWN_USER", "30"))  # 30s for known users
REQUEST_TIMEOUT = 30
TEST_REAL_USER = os.environ.get("TEST_REAL_USER", "")

# Conversation direction (string values in Dataverse)
DIRECTION_INBOUND = "Inbound"
DIRECTION_OUTBOUND = "Outbound"

# Conversation status (string values in Dataverse)
STATUS_UNCLAIMED = "Unclaimed"
STATUS_CLAIMED = "Claimed"
STATUS_PROCESSED = "Processed"

# Single fallback message for when Claude CLI is unavailable
FALLBACK_MESSAGE = "The system is temporarily unavailable, please try again shortly."

MS_TENANT_ID = "72f988bf-86f1-41af-91ab-2d7cd011db47"


def get_credential():
    """Get Azure credential, falling back to device code flow if needed.

    Tries ``DefaultAzureCredential`` first (works when ``az login`` session
    exists).  If that fails, starts a ``DeviceCodeCredential`` flow so the
    Global Manager can run on a fresh machine without ``az login``.

    The returned credential object lives in-memory -- it does **not** modify
    any system-wide state -- so multiple GM processes can each hold their own
    independent credentials simultaneously.
    """
    try:
        cred = DefaultAzureCredential()
        # Test whether the credential actually works
        cred.get_token("https://management.azure.com/.default")
        print("[AUTH] Using existing Azure credentials")
        return cred
    except Exception:
        print("[AUTH] No existing Azure credentials found. Starting device code authentication...")
        print("[AUTH] This is a one-time setup. You will be prompted to authenticate.")

        def device_code_callback(verification_uri, user_code, expires_in):
            print(f"\n[AUTH] ============================================")
            print(f"[AUTH] DEVICE CODE AUTHENTICATION REQUIRED")
            print(f"[AUTH] ============================================")
            print(f"[AUTH] Open this URL: {verification_uri}")
            print(f"[AUTH] Enter code: {user_code}")
            print(f"[AUTH] Expires in: {expires_in // 60} minutes")
            print(f"[AUTH] ============================================\n")

        cred = DeviceCodeCredential(
            tenant_id=MS_TENANT_ID,
            prompt_callback=device_code_callback,
        )
        # Force initial authentication
        cred.get_token("https://management.azure.com/.default")
        print("[AUTH] Device code authentication successful!")
        return cred


# ── Tool definitions for Claude ──────────────────────────────────────────

# Each tool is a dict with name, description, and a reference to the method
# that implements it.  The _execute_tools method dispatches tool calls from
# Claude's JSON response.

TOOL_DEFINITIONS = [
    {
        "name": "get_user_state",
        "description": (
            "Read the current onboarding state for a user from Dataverse. "
            "Returns the full user row including onboarding_step, devbox_name, "
            "azure_ad_id, connection_url, etc. Returns null if user not found."
        ),
        "parameters": {
            "user_email": "string -- the user's email address",
        },
    },
    {
        "name": "update_user_state",
        "description": (
            "Create or update the user's state in Dataverse. Pass any fields "
            "to update (e.g. crb3b_onboardingstep, crb3b_devboxname, etc.)."
        ),
        "parameters": {
            "user_email": "string -- the user's email address",
            "fields": "dict -- Dataverse fields to set",
        },
    },
    {
        "name": "check_devbox_status",
        "description": (
            "Check the current provisioning state of a user's dev box. Returns "
            "provisioning_state (e.g. Succeeded, Failed, Creating), status, "
            "and the direct web RDP connection URL."
        ),
        "parameters": {
            "devbox_name": "string -- name of the dev box",
            "azure_ad_id": "string -- user's Azure AD object ID",
        },
    },
    {
        "name": "get_rdp_auth_message",
        "description": (
            "Build an RDP-based authentication message that guides the user "
            "through connecting to their dev box and running claude /login. "
            "Returns the full formatted message text."
        ),
        "parameters": {
            "connection_url": "string -- web RDP connection URL for the dev box",
        },
    },
    {
        "name": "mark_user_onboarded",
        "description": (
            "Mark the user as fully onboarded. Updates Dataverse to 'completed' "
            "and adds them to the known users list."
        ),
        "parameters": {
            "user_email": "string -- the user's email address",
        },
    },
    {
        "name": "send_followup",
        "description": (
            "Send a follow-up message to the user via the conversations table. "
            "Use this when you need to send an intermediate message before your "
            "final response."
        ),
        "parameters": {
            "text": "string -- the message text to send",
        },
    },
]


class GlobalManager:
    """Agentic fallback manager for orphaned messages and new user onboarding.

    Instead of a hardcoded state machine, this class provides Claude with
    tools and a system prompt.  Claude decides what actions to take based
    on the user's message and their current state.
    """

    def __init__(self):
        self.manager_id = "global"
        self.credential = get_credential()
        self._token_cache = None
        self._token_expires = None
        # Track known users with personal managers
        self._known_users: set[str] = set()

        # DevBox provisioning (optional - only if env vars set)
        self.devcenter_endpoint = os.environ.get("DEVCENTER_ENDPOINT")
        self.devbox_project = os.environ.get("DEVBOX_PROJECT")
        self.devbox_pool = os.environ.get("DEVBOX_POOL", "shraga-worker-pool")
        self.devbox_manager = None
        if self.devcenter_endpoint and self.devbox_project:
            self.devbox_manager = DevBoxManager(
                devcenter_endpoint=self.devcenter_endpoint,
                project_name=self.devbox_project,
                pool_name=self.devbox_pool,
                credential=self.credential,
            )

        # Track onboarding state per user email
        self._onboarding_state: dict[str, dict] = {}

    # ── Auth ──────────────────────────────────────────────────────────

    def get_token(self) -> str | None:
        try:
            if self._token_cache and self._token_expires:
                if datetime.now(timezone.utc) < self._token_expires:
                    return self._token_cache
            token: AccessToken = self.credential.get_token(f"{DATAVERSE_URL}/.default")
            self._token_cache = token.token
            self._token_expires = (
                datetime.fromtimestamp(token.expires_on, tz=timezone.utc)
                - timedelta(minutes=5)
            )
            return self._token_cache
        except Exception as e:
            print(f"[ERROR] Getting token: {e}")
            return None

    # ── Azure AD ID Resolution ─────────────────────────────────────

    def resolve_azure_ad_id(self, email: str) -> str:
        """Resolve a user email to an Azure AD object ID (GUID) via Microsoft Graph.

        Calls GET https://graph.microsoft.com/v1.0/users/{email} and returns
        the ``id`` field.  Uses ``DefaultAzureCredential`` to acquire a token
        for the ``https://graph.microsoft.com/.default`` scope.

        Raises:
            Exception: when the Graph API call fails or the response does not
                contain an ``id`` field.
        """
        graph_token = self.credential.get_token("https://graph.microsoft.com/.default")
        headers = {
            "Authorization": f"Bearer {graph_token.token}",
            "Accept": "application/json",
        }
        url = f"https://graph.microsoft.com/v1.0/users/{email}"
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)

        if resp.status_code != 200:
            raise Exception(
                f"Graph API error resolving {email}: {resp.status_code} {resp.text}"
            )

        data = resp.json()
        aad_id = data.get("id")
        if not aad_id:
            raise Exception(f"Graph API response for {email} missing 'id' field")
        return aad_id

    def _headers(self, content_type=None, etag=None):
        token = self.get_token()
        if not token:
            return None
        h = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "OData-MaxVersion": "4.0",
            "OData-Version": "4.0",
        }
        if content_type:
            h["Content-Type"] = content_type
        if etag:
            h["If-Match"] = etag
        return h

    # ── User State (Dataverse persistence) ─────────────────────────────

    def _get_user_state(self, user_email: str) -> dict | None:
        """Query crb3b_shragausers for a user by email.

        Returns the full row as a dict, or None if the user is not found.
        Also populates the in-memory cache on hit.
        """
        headers = self._headers()
        if not headers:
            return None
        try:
            url = (
                f"{DATAVERSE_API}/{USERS_TABLE}"
                f"?$filter=crb3b_useremail eq '{user_email}'"
                f"&$top=1"
            )
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            rows = resp.json().get("value", [])
            if rows:
                row = rows[0]
                # Refresh in-memory cache from persisted state
                self._onboarding_state[user_email] = {
                    "dv_row_id": row.get("crb3b_shragauserid"),
                    "provisioning_started": row.get("crb3b_onboardingstep") in (
                        "provisioning", "waiting_provisioning",
                        "auth_pending", "auth_code_sent", "completed",
                    ),
                    "provisioning_complete": row.get("crb3b_onboardingstep") in (
                        "auth_pending", "auth_code_sent", "completed",
                    ),
                    "auth_complete": row.get("crb3b_onboardingstep") == "completed",
                    "devbox_name": row.get("crb3b_devboxname", ""),
                    "azure_ad_id": row.get("crb3b_azureadid", ""),
                    "connection_url": row.get("crb3b_connectionurl", ""),
                    "auth_url": row.get("crb3b_authurl", ""),
                    "onboarding_step": row.get("crb3b_onboardingstep", ""),
                }
                return row
            return None
        except requests.exceptions.Timeout:
            print(f"[WARN] _get_user_state timed out for {user_email}")
            return None
        except Exception as e:
            print(f"[ERROR] _get_user_state for {user_email}: {e}")
            return None

    def _update_user_state(self, user_email: str, **fields) -> bool:
        """Create or update a user row in the crb3b_shragausers Dataverse table.

        If the user already exists (by cached row id or lookup), PATCH the row.
        Otherwise, POST to create a new row.
        Returns True on success, False on failure.
        """
        headers = self._headers(content_type="application/json")
        if not headers:
            return False

        # Always include last-seen timestamp
        fields.setdefault("crb3b_lastseen", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

        # Try to resolve existing row id from cache or lookup
        cached = self._onboarding_state.get(user_email, {})
        row_id = cached.get("dv_row_id")

        if not row_id:
            # Attempt a lookup in DV
            existing = self._get_user_state(user_email)
            if existing:
                row_id = existing.get("crb3b_shragauserid")

        try:
            if row_id:
                # -- PATCH existing row --
                url = f"{DATAVERSE_API}/{USERS_TABLE}({row_id})"
                resp = requests.patch(url, headers=headers, json=fields, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
            else:
                # -- POST new row --
                fields["crb3b_useremail"] = user_email
                url = f"{DATAVERSE_API}/{USERS_TABLE}"
                resp = requests.post(url, headers=headers, json=fields, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                # Cache the new row id (Dataverse may return 204 No Content)
                if resp.status_code == 204 or not resp.content:
                    new_id = None
                else:
                    new_row = resp.json()
                    new_id = new_row.get("crb3b_shragauserid")
                if new_id:
                    cached["dv_row_id"] = new_id
                    self._onboarding_state[user_email] = cached

            return True
        except Exception as e:
            print(f"[ERROR] _update_user_state for {user_email}: {e}")
            return False

    # ── Conversations ─────────────────────────────────────────────────

    def poll_stale_unclaimed(self) -> list[dict]:
        """Poll for unclaimed inbound messages with differential delay.

        New users (not in crb3b_shragausers): claimed after CLAIM_DELAY_NEW_USER (default 0s).
        Known users: claimed after CLAIM_DELAY_KNOWN_USER (default 30s) to give
        the personal manager more time.
        """
        headers = self._headers()
        if not headers:
            return []
        try:
            # Use the shorter delay (new user) as the base cutoff to catch all
            # potentially claimable messages. Per-user delay is applied below.
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=CLAIM_DELAY_NEW_USER)
            cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
            url = (
                f"{DATAVERSE_API}/{CONVERSATIONS_TABLE}"
                f"?$filter=cr_direction eq '{DIRECTION_INBOUND}'"
                f" and cr_status eq '{STATUS_UNCLAIMED}'"
                f" and createdon lt {cutoff_str}"
                f"&$orderby=createdon asc"
                f"&$top=10"
            )
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            all_unclaimed = resp.json().get("value", [])

            if not all_unclaimed:
                return []

            # Apply differential delay: new users get immediate pickup,
            # known users wait CLAIM_DELAY_KNOWN_USER seconds
            now = datetime.now(timezone.utc)
            known_cutoff = now - timedelta(seconds=CLAIM_DELAY_KNOWN_USER)
            claimable = []
            for msg in all_unclaimed:
                user_email = msg.get("cr_useremail", "")
                is_known = user_email in self._known_users
                if not is_known:
                    # Quick check: is this user in the DV users table?
                    existing = self._get_user_state(user_email)
                    is_known = existing is not None
                    if is_known:
                        self._known_users.add(user_email)

                if not is_known:
                    # New user -> claim immediately
                    claimable.append(msg)
                else:
                    # Known user -> only claim if old enough
                    created_str = msg.get("createdon", "")
                    try:
                        created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                        if created < known_cutoff:
                            claimable.append(msg)
                    except (ValueError, TypeError):
                        # Can't parse date -> apply default delay
                        claimable.append(msg)

            return claimable
        except requests.exceptions.Timeout:
            print("[WARN] poll_stale_unclaimed timed out")
            return []
        except Exception as e:
            print(f"[ERROR] poll_stale_unclaimed: {e}")
            return []

    def claim_message(self, msg: dict) -> bool:
        row_id = msg.get("cr_shraga_conversationid")
        etag = msg.get("@odata.etag")
        if not row_id or not etag:
            return False
        headers = self._headers(content_type="application/json", etag=etag)
        if not headers:
            return False
        try:
            url = f"{DATAVERSE_API}/{CONVERSATIONS_TABLE}({row_id})"
            body = {
                "cr_status": STATUS_CLAIMED,
                "cr_claimed_by": f"{self.manager_id}:{INSTANCE_ID}",
            }
            resp = requests.patch(url, headers=headers, json=body, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 412:
                return False
            resp.raise_for_status()
            return True
        except Exception as e:
            print(f"[ERROR] claim_message: {e}")
            return False

    def mark_processed(self, row_id: str):
        headers = self._headers(content_type="application/json")
        if not headers:
            return
        try:
            url = f"{DATAVERSE_API}/{CONVERSATIONS_TABLE}({row_id})"
            requests.patch(
                url, headers=headers,
                json={"cr_status": STATUS_PROCESSED},
                timeout=REQUEST_TIMEOUT,
            )
            print(f"[DV] Marked {row_id[:8]} as Processed")
        except Exception as e:
            print(f"[WARN] mark_processed failed: {e}")

    def send_response(self, in_reply_to: str, mcs_conversation_id: str,
                      user_email: str, text: str, followup_expected: bool = False):
        headers = self._headers(content_type="application/json")
        if not headers:
            return None
        try:
            body = {
                "cr_name": text[:200],
                "cr_useremail": user_email,
                "cr_mcs_conversation_id": mcs_conversation_id,
                "cr_message": text,
                "cr_direction": DIRECTION_OUTBOUND,
                "cr_status": STATUS_UNCLAIMED,
                "cr_in_reply_to": in_reply_to,
                "cr_followup_expected": "true" if followup_expected else "",
            }
            url = f"{DATAVERSE_API}/{CONVERSATIONS_TABLE}"
            resp = requests.post(url, headers=headers, json=body, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            print(f"[DV] Wrote outbound response to {user_email} (reply_to={in_reply_to[:8]}): \"{text[:60]}...\"")
            if resp.status_code == 204 or not resp.content:
                return True
            return resp.json()
        except Exception as e:
            print(f"[ERROR] send_response: {e}")
            return None

    # ── Tool Implementations ─────────────────────────────────────────

    def _tool_get_user_state(self, user_email: str) -> dict:
        """Tool: read user state from Dataverse."""
        row = self._get_user_state(user_email)
        state = self._onboarding_state.get(user_email, {})
        if row:
            return {
                "found": True,
                "onboarding_step": row.get("crb3b_onboardingstep", ""),
                "devbox_name": row.get("crb3b_devboxname", ""),
                "azure_ad_id": row.get("crb3b_azureadid", ""),
                "connection_url": row.get("crb3b_connectionurl", ""),
                "auth_complete": state.get("auth_complete", False),
                "provisioning_started": state.get("provisioning_started", False),
                "provisioning_complete": state.get("provisioning_complete", False),
                "customization_started": state.get("customization_started", False),
                "customization_complete": state.get("customization_complete", False),
            }
        return {"found": False}

    def _tool_update_user_state(self, user_email: str, fields: dict) -> dict:
        """Tool: update user state in Dataverse."""
        ok = self._update_user_state(user_email, **fields)
        return {"success": ok}

    def _tool_provision_devbox(self, user_email: str) -> dict:
        """Tool: provision a dev box for the user."""
        if not self.devbox_manager:
            return {"error": "Dev box provisioning is not configured."}

        state = self._onboarding_state.get(user_email, {})
        try:
            user_azure_ad_id = state.get("azure_ad_id", "")
            if not user_azure_ad_id:
                resolve_email = TEST_REAL_USER if TEST_REAL_USER else user_email
                user_azure_ad_id = self.resolve_azure_ad_id(resolve_email)

            result = self.devbox_manager.provision_devbox(user_azure_ad_id, user_email)
            devbox_name = result.get("name", f"shraga-{user_email.split('@')[0].replace('.', '-')}")

            state["provisioning_started"] = True
            state["devbox_name"] = devbox_name
            state["azure_ad_id"] = user_azure_ad_id
            self._onboarding_state[user_email] = state

            self._update_user_state(
                user_email,
                crb3b_onboardingstep="provisioning",
                crb3b_devboxname=devbox_name,
                crb3b_azureadid=user_azure_ad_id,
            )

            return {
                "success": True,
                "devbox_name": devbox_name,
                "azure_ad_id": user_azure_ad_id,
            }
        except Exception as e:
            print(f"[ERROR] provision_devbox for {user_email}: {e}")
            return {"error": str(e)}

    def _tool_check_devbox_status(self, devbox_name: str, azure_ad_id: str) -> dict:
        """Tool: check dev box provisioning status."""
        if not self.devbox_manager:
            return {"error": "Dev box manager not configured."}
        try:
            info = self.devbox_manager.get_devbox_status(azure_ad_id, devbox_name)
            return {
                "provisioning_state": info.provisioning_state,
                "status": info.status,
                "connection_url": info.connection_url,
            }
        except Exception as e:
            return {"error": str(e)}

    def _tool_apply_customizations(self, devbox_name: str, azure_ad_id: str) -> dict:
        """Tool: apply software customizations to a dev box."""
        if not self.devbox_manager:
            return {"error": "Dev box manager not configured."}
        try:
            result = self.devbox_manager.apply_customizations(azure_ad_id, devbox_name)
            return {"success": True, "status": result.get("status", "Unknown")}
        except Exception as e:
            return {"error": str(e)}

    def _tool_check_customization_status(self, devbox_name: str, azure_ad_id: str) -> dict:
        """Tool: check customization status."""
        if not self.devbox_manager:
            return {"error": "Dev box manager not configured."}
        try:
            result = self.devbox_manager.get_customization_status(azure_ad_id, devbox_name)
            return {"status": result.get("status", "Unknown")}
        except Exception as e:
            return {"error": str(e)}

    def _tool_get_rdp_auth_message(self, connection_url: str) -> dict:
        """Tool: build RDP auth instructions for the user."""
        remote_auth = RemoteDevBoxAuth(connection_url=connection_url)
        return {"message": remote_auth.build_auth_message(connection_url)}

    def _tool_mark_user_onboarded(self, user_email: str) -> dict:
        """Tool: mark user as fully onboarded."""
        self._known_users.add(user_email)
        self._update_user_state(user_email, crb3b_onboardingstep="completed")
        self._onboarding_state.pop(user_email, None)
        return {"success": True}

    def _execute_tool(self, tool_name: str, tool_args: dict,
                      msg_context: dict) -> dict:
        """Dispatch a tool call and return the result as a dict."""
        user_email = msg_context.get("user_email", "")
        row_id = msg_context.get("row_id", "")
        mcs_conv_id = msg_context.get("mcs_conv_id", "")

        if tool_name == "get_user_state":
            return self._tool_get_user_state(
                tool_args.get("user_email", user_email),
            )
        elif tool_name == "update_user_state":
            return self._tool_update_user_state(
                tool_args.get("user_email", user_email),
                tool_args.get("fields", {}),
            )
        elif tool_name == "check_devbox_status":
            return self._tool_check_devbox_status(
                tool_args.get("devbox_name", ""),
                tool_args.get("azure_ad_id", ""),
            )
        elif tool_name == "get_rdp_auth_message":
            return self._tool_get_rdp_auth_message(
                tool_args.get("connection_url", ""),
            )
        elif tool_name == "mark_user_onboarded":
            return self._tool_mark_user_onboarded(
                tool_args.get("user_email", user_email),
            )
        elif tool_name == "send_followup":
            text = tool_args.get("text", "")
            if text:
                self.send_response(
                    in_reply_to=row_id,
                    mcs_conversation_id=mcs_conv_id,
                    user_email=user_email,
                    text=text,
                    followup_expected=True,
                )
            return {"sent": True}
        else:
            return {"error": f"Unknown tool: {tool_name}"}

    # ── Claude CLI ───────────────────────────────────────────────────

    def _call_claude(self, user_text: str, system_prompt: str) -> str | None:
        """Low-level Claude CLI call. Returns response text or None on failure."""
        cmd = [
            "claude",
            "--print",
            "--dangerously-skip-permissions",
            "--append-system-prompt", system_prompt,
            user_text,
        ]
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120, env=env,
            )
            if result.returncode != 0:
                print(f"[WARN] Claude CLI failed (rc={result.returncode}): {result.stderr[:300]}")
                return None
            return result.stdout.strip() or None
        except subprocess.TimeoutExpired:
            print("[WARN] Claude CLI timed out")
            return None
        except FileNotFoundError:
            print("[WARN] Claude CLI not found")
            return None
        except Exception as e:
            print(f"[ERROR] _call_claude: {e}")
            return None

    def _call_claude_with_tools(self, user_text: str, system_prompt: str,
                                msg_context: dict,
                                max_rounds: int = 5) -> str | None:
        """Call Claude in an agentic loop, executing tool calls until Claude
        produces a final text response.

        Claude's response is expected as JSON with either:
          {"response": "text to send to user"}
        or:
          {"tool_calls": [{"name": "...", "arguments": {...}}, ...]}

        Returns the final response text or None on failure.
        """
        tool_descriptions = "\n".join(
            f"- {t['name']}: {t['description']}"
            for t in TOOL_DEFINITIONS
        )

        full_prompt = (
            f"{user_text}\n\n"
            f"Available tools:\n{tool_descriptions}\n\n"
            f"Respond with JSON. Either:\n"
            f'  {{"response": "your message to the user"}}\n'
            f"or:\n"
            f'  {{"tool_calls": [{{"name": "tool_name", "arguments": {{...}}}}]}}\n\n'
            f"You can call multiple tools in sequence. After each round of tool "
            f"results, decide whether to call more tools or return a final response."
        )

        accumulated_context = full_prompt
        for _round in range(max_rounds):
            raw = self._call_claude(accumulated_context, system_prompt)
            if raw is None:
                return None

            # Try to parse as JSON
            parsed = self._try_parse_json(raw)
            if parsed is None:
                # Claude returned plain text -- use it as the response
                return raw

            # Check for final response
            if "response" in parsed:
                return parsed["response"]

            # Execute tool calls
            tool_calls = parsed.get("tool_calls", [])
            if not tool_calls:
                # No tools and no response -- treat raw as response
                return raw

            tool_results = []
            for tc in tool_calls:
                name = tc.get("name", "")
                args = tc.get("arguments", {})
                print(f"[TOOL] Executing: {name}({json.dumps(args)[:100]})")
                result = self._execute_tool(name, args, msg_context)
                tool_results.append({"tool": name, "result": result})
                print(f"[TOOL] Result: {json.dumps(result)[:200]}")

            # Feed results back to Claude for the next round, preserving
            # the original prompt context so Claude doesn't lose track
            accumulated_context = (
                f"ORIGINAL REQUEST:\n{full_prompt}\n\n"
                f"---\n\n"
                f"Tool results from the tools you just called:\n"
                f"{json.dumps(tool_results, default=str)}\n\n"
                f"Based on these results, decide your next action. Respond with JSON:\n"
                f'  {{"response": "your message to the user"}}  -- if you are done\n'
                f'  {{"tool_calls": [...]}}  -- if you need to call more tools'
            )

        # Exhausted rounds -- ask Claude for a final response
        final = self._call_claude(
            "You have used all available tool rounds. Please provide your FINAL "
            "response to the user now as a simple text message. Do NOT include "
            "any JSON, tool calls, or reasoning -- just the message to send to "
            "the user. Be friendly and concise.",
            system_prompt,
        )
        # If final response still contains JSON, try to extract it
        if final:
            parsed = self._try_parse_json(final)
            if parsed and "response" in parsed:
                return parsed["response"]
        return final

    def _try_parse_json(self, text: str) -> dict | None:
        """Try to parse text as JSON. Returns None if not valid JSON.

        Handles several common Claude response patterns:
        - Pure JSON: {"response": "..."}
        - Markdown code blocks: ```json\n{...}\n```
        - Mixed text + JSON: "Some text\n\n{"tool_calls": [...]}"
        """
        text = text.strip()
        # Handle markdown code blocks
        if text.startswith("```"):
            lines = text.split("\n")
            inner = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
            text = inner.strip()
        # Try parsing the full text first
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            pass
        # Try extracting JSON object from within the text (Claude sometimes
        # prefixes JSON with explanatory text)
        brace_idx = text.find("{")
        if brace_idx > 0:
            candidate = text[brace_idx:]
            try:
                return json.loads(candidate)
            except (json.JSONDecodeError, ValueError):
                pass
        return None

    # ── Message Processing (Agentic) ──────────────────────────────────

    def _build_system_prompt(self, user_email: str, has_devbox_manager: bool) -> str:
        """Build the system prompt for Claude based on current context."""
        devbox_note = (
            "You can check dev box status and guide users through authentication."
            if has_devbox_manager else
            "Dev box management is NOT configured on this instance."
        )

        return (
            "You are the Global Manager for Shraga, an AI-powered development "
            "platform. You help new users get set up and handle messages when "
            "a user's personal task manager is offline.\n\n"
            f"{devbox_note}\n\n"
            "GUIDELINES:\n"
            "- Be conversational and helpful, like a friendly colleague.\n"
            "- You can answer questions, have a conversation, and explain how "
            "the system works.\n"
            "- For new users: check their state, guide them to run setup.ps1 "
            "themselves (you CANNOT provision dev boxes -- the user must sign "
            "into Azure themselves and run: "
            "irm https://raw.githubusercontent.com/SagiKat/shraga-worker/main/setup.ps1 | iex). "
            "Monitor their provisioning progress with check_devbox_status, and "
            "when the dev box is ready, use get_rdp_auth_message to send them "
            "the RDP link with auth instructions.\n"
            "- For users whose personal manager is offline: acknowledge the issue "
            "and let them know you are handling it.\n"
            "- Do NOT use markdown formatting -- respond in plain text only.\n"
            "- When authentication is needed, use get_rdp_auth_message to get "
            "the full instructions (it includes the direct RDP link and tells "
            "the user to double-click the Shraga-Authenticate shortcut).\n"
            "- When a user confirms they completed setup (e.g., 'done', 'yes', "
            "'finished', etc.), use mark_user_onboarded to complete onboarding.\n\n"
            "IMPORTANT: Respond ONLY with JSON as instructed. Do NOT include "
            "any explanation, reasoning, or thinking before or after the JSON. "
            "Your entire response must be valid JSON -- either "
            "{\"response\": \"...\"} or {\"tool_calls\": [...]}. Nothing else."
        )

    def _build_user_context(self, msg: dict) -> str:
        """Build the context string Claude will receive about this message."""
        user_email = msg.get("cr_useremail", "")
        user_text = msg.get("cr_message", "").strip()
        is_known = user_email in self._known_users
        state = self._onboarding_state.get(user_email, {})

        context_parts = [
            f"User email: {user_email}",
            f"User message: \"{user_text}\"",
            f"Known user (has personal manager): {is_known}",
        ]

        if state:
            context_parts.append(f"Cached onboarding state: {json.dumps(state, default=str)}")

        return "\n".join(context_parts)

    def process_message(self, msg: dict):
        """Process an orphaned message using the agentic approach.

        Claude receives the user's message, their current state, and available
        tools.  Claude decides what to do -- no hardcoded pipeline.
        """
        row_id = msg.get("cr_shraga_conversationid")
        user_email = msg.get("cr_useremail", "")
        mcs_conv_id = msg.get("cr_mcs_conversation_id", "")
        user_text = msg.get("cr_message", "").strip()

        if not user_text:
            self.mark_processed(row_id)
            return

        print(f"[GLOBAL] Processing orphaned message from {user_email}: {user_text[:80]}...")

        # Build context for Claude
        user_context = self._build_user_context(msg)
        system_prompt = self._build_system_prompt(
            user_email, has_devbox_manager=bool(self.devbox_manager),
        )

        msg_context = {
            "user_email": user_email,
            "row_id": row_id,
            "mcs_conv_id": mcs_conv_id,
        }

        # Let Claude decide what to do
        response = self._call_claude_with_tools(
            user_context, system_prompt, msg_context,
        )

        if not response:
            response = FALLBACK_MESSAGE

        print(f"[PROCESS] Finished processing {row_id[:8]}. Sending response ({len(response)} chars)...")
        self.send_response(
            in_reply_to=row_id,
            mcs_conversation_id=mcs_conv_id,
            user_email=user_email,
            text=response,
        )
        self.mark_processed(row_id)
        print(f"[PROCESS] Done with {row_id[:8]}")

    # ── Main Loop ─────────────────────────────────────────────────────

    def run(self):
        if sys.platform == "win32":
            sys.stdout.reconfigure(encoding='utf-8')
            sys.stderr.reconfigure(encoding='utf-8')
        print(f"[START] Global Manager (agentic) | instance={INSTANCE_ID} | pid={os.getpid()}")
        print(f"[CONFIG] Dataverse: {DATAVERSE_URL}")
        print(f"[CONFIG] Users table: {USERS_TABLE}")
        print(f"[CONFIG] Claim delay: new users={CLAIM_DELAY_NEW_USER}s, known users={CLAIM_DELAY_KNOWN_USER}s")
        print(f"[CONFIG] Poll interval: {POLL_INTERVAL}s")

        while True:
            try:
                messages = self.poll_stale_unclaimed()
                if messages:
                    print(f"[POLL] Found {len(messages)} unclaimed message(s)")
                for msg in messages:
                    row_id = msg.get("cr_shraga_conversationid", "?")[:8]
                    user_email = msg.get("cr_useremail", "?")
                    user_text = (msg.get("cr_message", "") or "")[:50]
                    print(f"[CLAIM] Attempting to claim {row_id} from {user_email}: \"{user_text}\"")
                    if self.claim_message(msg):
                        print(f"[CLAIM] Claimed {row_id} successfully")
                        try:
                            self.process_message(msg)
                        except Exception as e:
                            row_id = msg.get("cr_shraga_conversationid", "?")
                            print(f"[ERROR] Processing message {row_id}: {e}")
                            try:
                                self.send_response(
                                    in_reply_to=row_id,
                                    mcs_conversation_id=msg.get("cr_mcs_conversation_id", ""),
                                    user_email=msg.get("cr_useremail", ""),
                                    text=FALLBACK_MESSAGE,
                                )
                                self.mark_processed(row_id)
                            except Exception:
                                pass

                time.sleep(POLL_INTERVAL)

            except KeyboardInterrupt:
                print("\n[STOP] Shutting down.")
                break
            except Exception as e:
                print(f"[ERROR] Main loop: {e}")
                time.sleep(POLL_INTERVAL * 2)


def main():
    manager = GlobalManager()
    manager.run()


if __name__ == "__main__":
    main()
