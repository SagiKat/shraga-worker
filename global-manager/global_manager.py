"""
Global Manager — Fallback Claude Code instance that handles orphaned messages.

Polls the conversations table for any unclaimed inbound messages older than 15s.
Handles new user onboarding (dev box provisioning) and users whose personal
manager is down.
"""
import requests
import json
import time
import os
import sys
import subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta
from azure.identity import DefaultAzureCredential, DeviceCodeCredential
from azure.core.credentials import AccessToken

sys.path.insert(0, str(Path(__file__).parent.parent))
from orchestrator_devbox import DevBoxManager, DevBoxInfo
from claude_auth_teams import ClaudeAuthManager, TeamsClaudeAuth, RemoteDevBoxAuth


DATAVERSE_URL = os.environ.get("DATAVERSE_URL", "https://org3e79cdb1.crm3.dynamics.com")
DATAVERSE_API = f"{DATAVERSE_URL}/api/data/v9.2"
CONVERSATIONS_TABLE = os.environ.get("CONVERSATIONS_TABLE", "cr_shraga_conversations")
TASKS_TABLE = os.environ.get("TASKS_TABLE", "cr_shraga_tasks")
USERS_TABLE = os.environ.get("USERS_TABLE", "crb3b_shragausers")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "5"))  # seconds
CLAIM_DELAY = int(os.environ.get("CLAIM_DELAY", "15"))  # seconds before global claims
REQUEST_TIMEOUT = 30
TEST_REAL_USER = os.environ.get("TEST_REAL_USER", "")

# Conversation direction (string values in Dataverse)
DIRECTION_INBOUND = "Inbound"
DIRECTION_OUTBOUND = "Outbound"

# Conversation status (string values in Dataverse)
STATUS_UNCLAIMED = "Unclaimed"
STATUS_CLAIMED = "Claimed"
STATUS_PROCESSED = "Processed"


MS_TENANT_ID = "72f988bf-86f1-41af-91ab-2d7cd011db47"


def get_credential():
    """Get Azure credential, falling back to device code flow if needed.

    Tries ``DefaultAzureCredential`` first (works when ``az login`` session
    exists).  If that fails, starts a ``DeviceCodeCredential`` flow so the
    Global Manager can run on a fresh machine without ``az login``.

    The returned credential object lives in-memory — it does **not** modify
    any system-wide state — so multiple GM processes can each hold their own
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


class GlobalManager:
    """Fallback manager for orphaned messages and new user onboarding."""

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
                # ── PATCH existing row ────────────────────────────────
                url = f"{DATAVERSE_API}/{USERS_TABLE}({row_id})"
                resp = requests.patch(url, headers=headers, json=fields, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
            else:
                # ── POST new row ──────────────────────────────────────
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
        """Poll for unclaimed inbound messages older than CLAIM_DELAY seconds."""
        headers = self._headers()
        if not headers:
            return []
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=CLAIM_DELAY)
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
            return resp.json().get("value", [])
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
                "cr_claimed_by": self.manager_id,
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
        except Exception as e:
            print(f"[WARN] mark_processed failed: {e}")

    def send_response(self, in_reply_to: str, mcs_conversation_id: str,
                      user_email: str, text: str):
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
            }
            url = f"{DATAVERSE_API}/{CONVERSATIONS_TABLE}"
            resp = requests.post(url, headers=headers, json=body, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            if resp.status_code == 204 or not resp.content:
                return True  # Success but no body (Dataverse returns 204 No Content)
            return resp.json()
        except Exception as e:
            print(f"[ERROR] send_response: {e}")
            return None

    # ── Message Processing ────────────────────────────────────────────

    def process_message(self, msg: dict):
        """Process an orphaned message.

        For now: check if user has a personal manager. If not, start onboarding.
        If yes, the personal manager is probably down — handle as fallback.
        """
        row_id = msg.get("cr_shraga_conversationid")
        user_email = msg.get("cr_useremail", "")
        mcs_conv_id = msg.get("cr_mcs_conversation_id", "")
        user_text = msg.get("cr_message", "").strip()

        if not user_text:
            self.mark_processed(row_id)
            return

        print(f"[GLOBAL] Processing orphaned message from {user_email}: {user_text[:80]}...")

        if user_email not in self._known_users:
            # Could be a new user or a user whose manager is down
            response = self._handle_potentially_new_user(msg)
        else:
            # Known user, personal manager must be down
            response = (
                "Your personal task manager appears to be offline. "
                "I'm the global fallback manager. I've noted your message and "
                "will try to restart your personal manager. "
                "In the meantime, please try again in a minute."
            )

        self.send_response(
            in_reply_to=row_id,
            mcs_conversation_id=mcs_conv_id,
            user_email=user_email,
            text=response,
        )
        self.mark_processed(row_id)

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
                cmd, capture_output=True, text=True, timeout=60, env=env,
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

    def _handle_potentially_new_user(self, msg: dict) -> str:
        """Handle a message from a potentially new user.

        Runs the full onboarding flow, persisting every state change to the
        crb3b_shragausers Dataverse table so progress survives restarts:
        1. Check if DevBoxManager is configured
        2. Provision a dev box for the user
        3. Monitor provisioning state
        4. Initiate Claude Code auth flow
        5. Fall back to RDP link if device code fails
        6. Hand off to personal manager when ready
        """
        user_email = msg.get("cr_useremail", "")
        user_text = msg.get("cr_message", "").strip()
        row_id = msg.get("cr_shraga_conversationid", "")
        mcs_conv_id = msg.get("cr_mcs_conversation_id", "")

        # ── Gate: DevBoxManager must be configured ──────────────────
        if not self.devbox_manager:
            return (
                "Dev box provisioning isn't configured yet. "
                "Please contact your admin."
            )

        onboarding_system_prompt = (
            "You are the Global Manager for Shraga, an AI-powered development platform. "
            "You help new users get set up with their personal dev box and Claude Code authentication. "
            "Keep responses concise, friendly, and informative. "
            "Guide the user through the onboarding process step by step. "
            "Do NOT use markdown formatting — respond in plain text only."
        )

        # ── Load persisted state from Dataverse (+ refresh cache) ──
        dv_row = self._get_user_state(user_email)
        state = self._onboarding_state.get(user_email, {})

        # Record every interaction
        self._update_user_state(user_email, crb3b_lastseen=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

        # ── Step 1: Start provisioning ──────────────────────────────
        if not state.get("provisioning_started"):
            # Let Claude compose a welcome message
            welcome_prompt = (
                f"A new user ({user_email}) just messaged: \"{user_text}\"\n"
                f"They don't have a dev box yet. Tell them you'll provision one now. "
                f"Be welcoming. Mention it may take a few minutes."
            )
            welcome = self._call_claude(welcome_prompt, onboarding_system_prompt)
            if not welcome:
                welcome = (
                    f"Welcome! I'm the Global Manager and I'll help you get set up. "
                    f"I'm provisioning a dev box for you now — this may take a few minutes."
                )

            # Send the welcome message immediately
            self.send_response(
                in_reply_to=row_id,
                mcs_conversation_id=mcs_conv_id,
                user_email=user_email,
                text=welcome,
            )

            # Kick off provisioning
            try:
                # Resolve the real Azure AD object ID (GUID) via Microsoft Graph
                user_azure_ad_id = state.get("azure_ad_id", "")
                if not user_azure_ad_id:
                    # For testing: resolve a real user email instead of the mock
                    resolve_email = TEST_REAL_USER if TEST_REAL_USER else user_email
                    user_azure_ad_id = self.resolve_azure_ad_id(resolve_email)
                result = self.devbox_manager.provision_devbox(user_azure_ad_id, user_email)
                devbox_name = result.get("name", f"shraga-{user_email.split('@')[0].replace('.', '-')}")

                state["provisioning_started"] = True
                state["devbox_name"] = devbox_name
                state["azure_ad_id"] = user_azure_ad_id
                self._onboarding_state[user_email] = state

                # Persist to Dataverse
                self._update_user_state(
                    user_email,
                    crb3b_onboardingstep="provisioning",
                    crb3b_devboxname=devbox_name,
                    crb3b_azureadid=user_azure_ad_id,
                )

                return (
                    f"Provisioning has started for your dev box ({devbox_name}). "
                    f"I'll check on the progress — feel free to message me anytime."
                )
            except Exception as e:
                print(f"[ERROR] provision_devbox for {user_email}: {e}")
                return (
                    f"I ran into a problem provisioning your dev box: {e}\n"
                    f"Please contact your admin or try again later."
                )

        # ── Step 2: Check provisioning status ───────────────────────
        devbox_name = state.get("devbox_name", "")
        user_azure_ad_id = state.get("azure_ad_id", "")

        if not state.get("provisioning_complete"):
            try:
                info = self.devbox_manager.get_devbox_status(user_azure_ad_id, devbox_name)

                if info.provisioning_state == "Succeeded":
                    state["provisioning_complete"] = True
                    state["connection_url"] = info.connection_url
                    self._onboarding_state[user_email] = state

                    # Persist to Dataverse
                    self._update_user_state(
                        user_email,
                        crb3b_onboardingstep="customizing",
                        crb3b_connectionurl=info.connection_url,
                    )

                    # Apply customizations (Git, Claude Code, Python)
                    try:
                        self.devbox_manager.apply_customizations(
                            user_azure_ad_id, devbox_name,
                        )
                        state["customization_started"] = True
                        self._onboarding_state[user_email] = state
                    except Exception as cust_err:
                        print(f"[WARN] apply_customizations for {user_email}: {cust_err}")
                        # Non-fatal — we can still continue to auth

                    progress_prompt = (
                        f"The user's dev box ({devbox_name}) just finished provisioning. "
                        f"Software customizations (Git, Claude Code, Python) are being installed. "
                        f"Tell them the good news and that the next step is authenticating Claude Code."
                    )
                    progress_msg = self._call_claude(progress_prompt, onboarding_system_prompt)
                    if not progress_msg:
                        progress_msg = (
                            f"Great news — your dev box ({devbox_name}) is ready! "
                            f"Installing developer tools now. "
                            f"Next step: let's authenticate Claude Code."
                        )
                    # Don't return yet — fall through to customization check below

                    self.send_response(
                        in_reply_to=row_id,
                        mcs_conversation_id=mcs_conv_id,
                        user_email=user_email,
                        text=progress_msg,
                    )

                elif info.provisioning_state == "Failed":
                    # Reset state so they can retry
                    self._onboarding_state.pop(user_email, None)
                    self._update_user_state(
                        user_email,
                        crb3b_onboardingstep="provisioning_failed",
                    )
                    return (
                        f"Unfortunately, your dev box provisioning failed. "
                        f"Please contact your admin or try again by sending me a message."
                    )

                else:
                    # Still in progress
                    self._update_user_state(
                        user_email,
                        crb3b_onboardingstep="waiting_provisioning",
                    )
                    return (
                        f"Your dev box ({devbox_name}) is still being provisioned "
                        f"(status: {info.provisioning_state}). "
                        f"Hang tight — I'll let you know as soon as it's ready."
                    )

            except Exception as e:
                print(f"[ERROR] get_devbox_status for {user_email}: {e}")
                return (
                    f"I couldn't check your dev box status right now: {e}\n"
                    f"Please try again in a moment."
                )

        # ── Step 2b: Monitor customization status ─────────────────────
        if state.get("customization_started") and not state.get("customization_complete"):
            try:
                cust_status = self.devbox_manager.get_customization_status(
                    user_azure_ad_id, devbox_name,
                )
                cust_state = cust_status.get("status", "Unknown")

                if cust_state == "Succeeded":
                    state["customization_complete"] = True
                    self._onboarding_state[user_email] = state
                    self._update_user_state(
                        user_email,
                        crb3b_onboardingstep="auth_pending",
                    )
                    # Fall through to auth step

                elif cust_state in ("Failed", "ValidationFailed"):
                    # Customization failed — still allow auth, just warn
                    print(f"[WARN] Customization {cust_state} for {devbox_name}")
                    state["customization_complete"] = True
                    self._onboarding_state[user_email] = state
                    self._update_user_state(
                        user_email,
                        crb3b_onboardingstep="auth_pending",
                    )
                    self.send_response(
                        in_reply_to=row_id,
                        mcs_conversation_id=mcs_conv_id,
                        user_email=user_email,
                        text=(
                            f"Some software customizations did not complete successfully. "
                            f"You may need to install tools manually later. "
                            f"Continuing with authentication."
                        ),
                    )
                    # Fall through to auth step

                else:
                    # Still running
                    return (
                        f"Your dev box ({devbox_name}) is being customized "
                        f"(installing developer tools, status: {cust_state}). "
                        f"Hang tight — almost there."
                    )

            except Exception as e:
                print(f"[WARN] get_customization_status for {user_email}: {e}")
                # If we can't check, assume done and move on
                state["customization_complete"] = True
                self._onboarding_state[user_email] = state
                self._update_user_state(
                    user_email,
                    crb3b_onboardingstep="auth_pending",
                )

        # ── Step 3: Send RDP link for auth on the dev box ──────────
        #
        # IMPORTANT: Claude Code must be authenticated on the TARGET DEV
        # BOX, not on the Global Manager's machine.  Instead of running
        # ``claude /login`` locally we send the user the web RDP URL so
        # they can open a browser-based session to the dev box and run
        # ``claude /login`` there.
        #
        onboarding_step = state.get("onboarding_step", "")

        if not state.get("auth_complete") and onboarding_step != "auth_pending_rdp":
            connection_url = state.get(
                "connection_url",
                f"https://devbox.microsoft.com/connect?devbox={devbox_name}",
            )

            # Build the setup instructions using RemoteDevBoxAuth
            remote_auth = RemoteDevBoxAuth(connection_url=connection_url)
            auth_message = remote_auth.build_auth_message(connection_url)

            state["onboarding_step"] = "auth_pending_rdp"
            self._onboarding_state[user_email] = state

            # Persist to Dataverse
            self._update_user_state(
                user_email,
                crb3b_onboardingstep="auth_pending",
                crb3b_connectionurl=connection_url,
            )

            return auth_message

        # ── Step 4: User confirms auth is done ─────────────────────
        if not state.get("auth_complete"):
            # Check if the user said "done" (or similar confirmation)
            user_lower = user_text.lower().strip()
            if user_lower in ("done", "yes", "completed", "finished", "ready", "ok"):
                state["auth_complete"] = True
                self._onboarding_state[user_email] = state
                self._known_users.add(user_email)

                # Persist completed state to Dataverse
                self._update_user_state(
                    user_email,
                    crb3b_onboardingstep="completed",
                )

                done_prompt = (
                    f"The user ({user_email}) has completed setup on their dev box "
                    f"({devbox_name}). Tell them their personal assistant is ready "
                    f"and they can start creating tasks."
                )
                done_msg = self._call_claude(done_prompt, onboarding_system_prompt)
                if not done_msg:
                    done_msg = (
                        f"Setup complete! Your personal assistant is ready. "
                        f"You can now create and manage coding tasks. Just tell me "
                        f"what you need!"
                    )

                # Clean up in-memory onboarding state (DV retains the record)
                self._onboarding_state.pop(user_email, None)

                return done_msg
            else:
                # User sent something else -- remind them
                connection_url = state.get(
                    "connection_url",
                    f"https://devbox.microsoft.com/connect?devbox={devbox_name}",
                )
                return (
                    f"I'm waiting for you to complete setup on your dev box.\n\n"
                    f"Please open this link: {connection_url}\n"
                    f"In the browser session, open PowerShell and run: claude /login\n\n"
                    f"Once you have completed authentication and setup, reply with "
                    f"**done**."
                )

        # ── Fallback ────────────────────────────────────────────────
        return (
            "I'm still setting up your environment. "
            "Please open the web RDP link I sent earlier and complete "
            "setup on your dev box. Reply with **done** when finished."
        )

    # ── Main Loop ─────────────────────────────────────────────────────

    def run(self):
        print(f"[START] Global Manager (fallback)")
        print(f"[CONFIG] Dataverse: {DATAVERSE_URL}")
        print(f"[CONFIG] Users table: {USERS_TABLE}")
        print(f"[CONFIG] Claim delay: {CLAIM_DELAY}s")
        print(f"[CONFIG] Poll interval: {POLL_INTERVAL}s")

        while True:
            try:
                messages = self.poll_stale_unclaimed()
                for msg in messages:
                    if self.claim_message(msg):
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
                                    text="Sorry, something went wrong. Please try again.",
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
