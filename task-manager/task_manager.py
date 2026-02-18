"""
Personal Task Manager -- Agentic architecture.

Polls the conversations table for unclaimed inbound messages from its user,
processes them via Claude with tools, and writes outbound responses.
Runs on the user's dev box.

Instead of a fixed ACTION: RESPOND/CREATE_TASK/CANCEL_TASK format, Claude
has tools it can call directly.  Claude decides freely what to do based on
the user's message, conversation history, and available tools.

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
import platform
import threading
import uuid

# Unique instance ID for this process
INSTANCE_ID = uuid.uuid4().hex[:8]
from pathlib import Path
from datetime import datetime, timezone, timedelta
from azure.identity import DefaultAzureCredential
from azure.core.credentials import AccessToken

# Import Dev Box manager (optional -- only needed for provision_devbox tool)
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from orchestrator_devbox import DevBoxManager
    DEVBOX_AVAILABLE = True
except ImportError:
    DEVBOX_AVAILABLE = False


DATAVERSE_URL = os.environ.get("DATAVERSE_URL", "https://org3e79cdb1.crm3.dynamics.com")
DATAVERSE_API = f"{DATAVERSE_URL}/api/data/v9.2"
CONVERSATIONS_TABLE = os.environ.get("CONVERSATIONS_TABLE", "cr_shraga_conversations")
TASKS_TABLE = os.environ.get("TASKS_TABLE", "cr_shraga_tasks")
MESSAGES_TABLE = os.environ.get("MESSAGES_TABLE", "cr_shragamessages")
USER_EMAIL = os.environ.get("USER_EMAIL")  # Required -- which user this manager serves
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "3"))  # seconds
REQUEST_TIMEOUT = 30
# Use a faster model for chat responses (latency-sensitive) while workers use the default model
CHAT_MODEL = os.environ.get("CHAT_MODEL", "")  # e.g., "claude-sonnet-4-5-20250929" for faster chat responses

# Conversation direction (string values in Dataverse)
DIRECTION_INBOUND = "Inbound"    # user -> manager
DIRECTION_OUTBOUND = "Outbound"  # manager -> user

# Conversation status (string values in Dataverse)
STATUS_UNCLAIMED = "Unclaimed"
STATUS_CLAIMED = "Claimed"
STATUS_PROCESSED = "Processed"
STATUS_DELIVERED = "Delivered"
STATUS_EXPIRED = "Expired"  # For stale outbound rows that were never delivered

# Task status codes (match integrated_task_worker.py)
TASK_PENDING = 1
TASK_QUEUED = 3
TASK_RUNNING = 5
TASK_WAITING = 6
TASK_COMPLETED = 7
TASK_FAILED = 8
TASK_CANCELED = 9

TASK_STATUS_NAMES = {
    TASK_PENDING: "Pending",
    TASK_QUEUED: "Queued",
    TASK_RUNNING: "Running",
    TASK_WAITING: "Waiting for Input",
    TASK_COMPLETED: "Completed",
    TASK_FAILED: "Failed",
    TASK_CANCELED: "Canceled",
}

# Single fallback message for when Claude CLI is unavailable
FALLBACK_MESSAGE = "The system is temporarily unavailable, please try again shortly."

WORKING_DIR = os.environ.get("WORKING_DIR", "")  # Dev box working directory for Claude


SESSIONS_FILE = os.environ.get("SESSIONS_FILE", "")  # Override path for session mapping

# Dev Box provisioning configuration (optional -- set to enable provision_devbox tool)
DEVCENTER_ENDPOINT = os.environ.get("DEVCENTER_ENDPOINT")
DEVBOX_PROJECT = os.environ.get("DEVBOX_PROJECT")
DEVBOX_POOL = os.environ.get("DEVBOX_POOL", "shraga-worker-pool")
USER_AZURE_AD_ID = os.environ.get("USER_AZURE_AD_ID", "")  # Azure AD object ID of the user


class TaskManager:
    """Agentic Personal Task Manager for a single user.

    Claude has tools to create tasks, cancel tasks, check status, list tasks,
    provision dev boxes, and respond to the user.  Claude decides what to do
    based on the user's message and context -- no hardcoded action parsing.
    """

    def __init__(self, user_email: str, working_dir: str = ""):
        if not user_email:
            raise ValueError("USER_EMAIL is required")
        self.user_email = user_email
        self.manager_id = f"personal:{user_email}"
        self.working_dir = working_dir or WORKING_DIR
        self.credential = DefaultAzureCredential()
        self._token_cache = None
        self._token_expires = None
        # MCS conversation ID -> Claude session ID mapping (persisted to disk)
        self._sessions_path = self._resolve_sessions_path()
        self._sessions: dict[str, str] = self._load_sessions()

    # ── Session Persistence ────────────────────────────────────────

    def _resolve_sessions_path(self) -> Path:
        """Determine where to store the session mapping file."""
        if SESSIONS_FILE:
            return Path(SESSIONS_FILE)
        # Default: ~/.shraga/sessions_{user}.json
        shraga_dir = Path.home() / ".shraga"
        shraga_dir.mkdir(exist_ok=True)
        safe_email = self.user_email.replace("@", "_at_").replace(".", "_")
        return shraga_dir / f"sessions_{safe_email}.json"

    def _load_sessions(self) -> dict[str, str]:
        """Load session mapping from disk. Returns empty dict on any failure."""
        try:
            if self._sessions_path.exists():
                data = json.loads(self._sessions_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    print(f"[SESSIONS] Loaded {len(data)} session(s) from {self._sessions_path}")
                    return data
        except Exception as e:
            print(f"[WARN] Failed to load sessions: {e}")
        return {}

    def _save_sessions(self):
        """Persist session mapping to disk."""
        try:
            self._sessions_path.write_text(
                json.dumps(self._sessions, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[WARN] Failed to save sessions: {e}")

    def _forget_session(self, mcs_conversation_id: str):
        """Remove a stale session and persist."""
        if mcs_conversation_id in self._sessions:
            old = self._sessions.pop(mcs_conversation_id)
            print(f"[SESSIONS] Forgot stale session {old[:8]}... for {mcs_conversation_id[:20]}...")
            self._save_sessions()

    # ── Auth ──────────────────────────────────────────────────────────

    def get_token(self) -> str | None:
        """Get OAuth token with caching."""
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

    def _headers(self, content_type=None, etag=None):
        """Build OData headers."""
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

    # ── Conversations CRUD ────────────────────────────────────────────

    def poll_unclaimed(self) -> list[dict]:
        """Poll for unclaimed inbound messages for this user."""
        headers = self._headers()
        if not headers:
            return []
        try:
            url = (
                f"{DATAVERSE_API}/{CONVERSATIONS_TABLE}"
                f"?$filter=cr_useremail eq '{self.user_email}'"
                f" and cr_direction eq '{DIRECTION_INBOUND}'"
                f" and cr_status eq '{STATUS_UNCLAIMED}'"
                f"&$orderby=createdon asc"
                f"&$top=10"
            )
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            messages = resp.json().get("value", [])
            if messages:
                print(f"[POLL] Found {len(messages)} unclaimed message(s) for {self.user_email}")
            return messages
        except requests.exceptions.Timeout:
            print("[WARN] poll_unclaimed timed out")
            return []
        except Exception as e:
            print(f"[ERROR] poll_unclaimed: {e}")
            return []

    def claim_message(self, msg: dict) -> bool:
        """Atomically claim a message using ETag optimistic concurrency."""
        row_id = msg.get("cr_shraga_conversationid")
        etag = msg.get("@odata.etag")
        if not row_id or not etag:
            print(f"[WARN] Cannot claim message -- missing id or etag")
            return False

        headers = self._headers(
            content_type="application/json",
            etag=etag,
        )
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
                # Someone else claimed it first (optimistic concurrency conflict)
                print(f"[INFO] Message {row_id} already claimed by another manager")
                return False
            resp.raise_for_status()
            print(f"[CLAIM] Claimed {row_id[:8]} successfully")
            return True
        except requests.exceptions.Timeout:
            print(f"[WARN] claim_message timed out for {row_id}")
            return False
        except Exception as e:
            print(f"[ERROR] claim_message: {e}")
            return False

    def mark_processed(self, row_id: str):
        """Mark inbound message as processed."""
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

    def send_response(self, in_reply_to: str, mcs_conversation_id: str, text: str,
                      followup_expected: bool = False):
        """Write an outbound response to the conversations table."""
        headers = self._headers(content_type="application/json")
        if not headers:
            print("[ERROR] Cannot send response -- no auth token")
            return None
        try:
            body = {
                "cr_name": text[:100],
                "cr_useremail": self.user_email,
                "cr_mcs_conversation_id": mcs_conversation_id,
                "cr_message": text,
                "cr_direction": DIRECTION_OUTBOUND,
                "cr_status": STATUS_UNCLAIMED,  # relay flow will pick this up
                "cr_in_reply_to": in_reply_to,
                "cr_followup_expected": "true" if followup_expected else "",
            }
            url = f"{DATAVERSE_API}/{CONVERSATIONS_TABLE}"
            resp = requests.post(url, headers=headers, json=body, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            print(f"[DV] Wrote outbound response (reply_to={in_reply_to[:8]}): \"{text[:60]}...\"")
            if resp.status_code == 204:
                return {"cr_shraga_conversationid": "created"}
            return resp.json()
        except Exception as e:
            print(f"[ERROR] send_response: {e}")
            return None

    # ── Stale Row Cleanup ─────────────────────────────────────────────

    def cleanup_stale_outbound(self, max_age_minutes: int = 10):
        """Mark old Unclaimed Outbound rows as Expired to prevent stale data issues.

        Per spec: old unclaimed Outbound rows from testing/crashes can interfere
        with the isFollowup filter. Uses 'Expired' status (not 'Delivered') to
        distinguish rows that timed out from rows actually delivered to users.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
        cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

        url = (
            f"{DATAVERSE_API}/{CONVERSATIONS_TABLE}"
            f"?$filter=cr_useremail eq '{self.user_email}'"
            f" and cr_direction eq '{DIRECTION_OUTBOUND}'"
            f" and cr_status eq '{STATUS_UNCLAIMED}'"
            f" and createdon lt {cutoff_str}"
            f"&$top=50"
        )

        headers = self._headers()
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            rows = resp.json().get("value", [])
        except Exception as e:
            print(f"[CLEANUP] Error querying stale rows: {e}")
            return 0

        if not rows:
            return 0

        cleaned = 0
        for row in rows:
            row_id = row.get("cr_shraga_conversationid")
            if not row_id:
                continue
            try:
                patch_url = f"{DATAVERSE_API}/{CONVERSATIONS_TABLE}({row_id})"
                patch_headers = self._headers(content_type="application/json")
                body = {"cr_status": STATUS_EXPIRED}
                resp = requests.patch(patch_url, headers=patch_headers, json=body, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                cleaned += 1
            except Exception as e:
                print(f"[CLEANUP] Error marking row {row_id} as Expired: {e}")

        if cleaned > 0:
            print(f"[CLEANUP] Marked {cleaned} stale outbound row(s) as Expired")
        return cleaned

    # ── Tasks CRUD ────────────────────────────────────────────────────

    def create_task(self, prompt: str, description: str = "") -> dict | None:
        """Create a new task in Dataverse. Returns the created row with task ID."""
        headers = self._headers(content_type="application/json")
        if not headers:
            return None
        try:
            body = {
                "cr_name": prompt[:100],  # Task title (primary name column, max 100 chars in DV)
                "cr_prompt": prompt,
                "cr_status": TASK_PENDING,
                "crb3b_useremail": self.user_email,
                "crb3b_devbox": platform.node(),
                "crb3b_workingdir": self.working_dir,  # Worker will override with OneDrive session folder path
            }
            if description:
                body["cr_result"] = ""  # Initialize empty
            # Ask DV to return the created entity so we get the task ID
            create_headers = {**headers, "Prefer": "return=representation"}
            url = f"{DATAVERSE_API}/{TASKS_TABLE}"
            resp = requests.post(url, headers=create_headers, json=body, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            if resp.status_code == 204:
                # Fallback: try to get ID from OData-EntityId header
                entity_id = resp.headers.get("OData-EntityId", "")
                task_id = ""
                if "(" in entity_id:
                    task_id = entity_id.split("(")[-1].rstrip(")")
                return {"cr_name": prompt, "cr_shraga_taskid": task_id}
            return resp.json()
        except Exception as e:
            # Log the full Dataverse error response for debugging
            error_detail = ""
            if hasattr(e, 'response') and e.response is not None:
                try:
                    error_detail = f" | Response: {e.response.text[:500]}"
                except Exception:
                    pass
            print(f"[ERROR] create_task: {e}{error_detail}")
            return None

    def wait_for_running_link(self, task_id: str, timeout: int = 18) -> str:
        """Poll a task row until the Running card message ID appears, then return the deep link.
        Returns empty string if not found within timeout."""
        if not task_id:
            return ""
        headers = self._headers()
        if not headers:
            return ""

        deadline = time.time() + timeout
        poll_interval = 3
        while time.time() < deadline:
            try:
                url = (
                    f"{DATAVERSE_API}/{TASKS_TABLE}({task_id})"
                    f"?$select=crb3b_runningchatid,crb3b_runningmessageid"
                )
                resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
                if resp.status_code == 200:
                    data = resp.json()
                    chat_id = data.get("crb3b_runningchatid", "")
                    msg_id = data.get("crb3b_runningmessageid", "")
                    if chat_id and msg_id:
                        deep_link = f"https://teams.microsoft.com/l/message/{chat_id}/{msg_id}"
                        print(f"[LINK] Running card link ready for task {task_id[:8]}...")
                        return deep_link
            except Exception:
                pass
            time.sleep(poll_interval)

        print(f"[LINK] Timed out waiting for Running card link for task {task_id[:8]}...")
        return ""

    def _monitor_task_start(self, task_id: str, task_title: str,
                            mcs_conversation_id: str, inbound_row_id: str):
        """Background thread: poll for the Running card, then call Claude to compose
        a natural follow-up message and send it proactively."""
        try:
            deep_link = self.wait_for_running_link(task_id, timeout=15)

            # Build a situational prompt for Claude to compose the follow-up naturally
            if deep_link:
                situation = (
                    f"[SYSTEM UPDATE] The task '{task_title}' has been queued. "
                    f"The live progress card is at: {deep_link}\n"
                    f"Send the user a natural follow-up message with this link. "
                    f"The card will update live as the worker picks it up and runs it. "
                    f"Keep it short and conversational."
                )
            else:
                situation = (
                    f"[SYSTEM UPDATE] The task '{task_title}' was submitted "
                    f"but the progress card isn't ready yet.\n"
                    f"Let the user know the task was queued and they'll get an update soon. "
                    f"Keep it brief and reassuring."
                )

            # Use a FRESH session for follow-ups (no --resume) to avoid
            # session context contamination (e.g., GM onboarding messages leaking in)
            system_prompt = (
                "You are following up on a task you helped the user submit. "
                "Respond directly to the user with a brief, natural message. "
                "Do NOT introduce yourself or mention onboarding. "
                "Just address the task status update."
            )

            followup_text, _ = self._call_claude(
                situation, system_prompt, session_id=None,
            )

            if not followup_text:
                followup_text = FALLBACK_MESSAGE

            # Send the follow-up as a proactive outbound message
            self.send_response(
                in_reply_to=inbound_row_id,
                mcs_conversation_id=mcs_conversation_id,
                text=followup_text,
            )
            print(f"[MONITOR] Follow-up sent for task {task_id[:8]}...")

        except Exception as e:
            print(f"[ERROR] _monitor_task_start: {e}")

    def list_tasks(self, top: int = 5) -> list[dict]:
        """List recent tasks for this user."""
        headers = self._headers()
        if not headers:
            return []
        try:
            url = (
                f"{DATAVERSE_API}/{TASKS_TABLE}"
                f"?$filter=crb3b_useremail eq '{self.user_email}'"
                f"&$orderby=createdon desc"
                f"&$top={top}"
                f"&$select=cr_shraga_taskid,cr_name,cr_prompt,cr_status,cr_result,createdon"
            )
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.json().get("value", [])
        except Exception as e:
            print(f"[ERROR] list_tasks: {e}")
            return []

    def get_task(self, task_id: str) -> dict | None:
        """Get a single task by ID."""
        headers = self._headers()
        if not headers:
            return None
        try:
            url = f"{DATAVERSE_API}/{TASKS_TABLE}({task_id})"
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"[ERROR] get_task: {e}")
            return None

    def cancel_task(self, task_id: str) -> bool:
        """Cancel a running task."""
        headers = self._headers(content_type="application/json")
        if not headers:
            return False
        try:
            url = f"{DATAVERSE_API}/{TASKS_TABLE}({task_id})"
            resp = requests.patch(
                url, headers=headers,
                json={"cr_status": TASK_CANCELED},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            print(f"[ERROR] cancel_task: {e}")
            return False

    def get_task_messages(self, task_id: str, top: int = 20) -> list[dict]:
        """Get recent messages for a task."""
        headers = self._headers()
        if not headers:
            return []
        try:
            url = (
                f"{DATAVERSE_API}/{MESSAGES_TABLE}"
                f"?$filter=crb3b_taskid eq '{task_id}'"
                f"&$orderby=createdon desc"
                f"&$top={top}"
                f"&$select=cr_name,cr_content,cr_from,createdon"
            )
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.json().get("value", [])
        except Exception as e:
            print(f"[ERROR] get_task_messages: {e}")
            return []

    # ── Tool Implementations ─────────────────────────────────────────

    def _tool_create_task(self, prompt: str, description: str = "",
                          mcs_conversation_id: str = "",
                          inbound_row_id: str = "") -> dict:
        """Tool: create a new task."""
        task = self.create_task(prompt=prompt, description=description)
        if task:
            new_task_id = task.get("cr_shraga_taskid", "")
            # Kick off background monitoring for the Running card
            if new_task_id and mcs_conversation_id:
                t = threading.Thread(
                    target=self._monitor_task_start,
                    args=(new_task_id, prompt, mcs_conversation_id, inbound_row_id),
                    daemon=True,
                )
                t.start()
                print(f"[MONITOR] Background thread started for task {new_task_id[:8]}...")
            return {
                "success": True,
                "task_id": new_task_id,
                "task_name": task.get("cr_name", prompt),
            }
        return {"success": False, "error": "Failed to create task in Dataverse"}

    def _tool_cancel_task(self, task_id: str, recent_tasks: list[dict] = None) -> dict:
        """Tool: cancel a task."""
        target_id = task_id
        if target_id == "latest" or not target_id:
            # Find most recent running task
            for t in (recent_tasks or []):
                if t.get("cr_status") == TASK_RUNNING:
                    target_id = t.get("cr_shraga_taskid")
                    break
        if target_id and target_id != "latest":
            ok = self.cancel_task(target_id)
            return {"success": ok, "task_id": target_id}
        return {"success": False, "error": "No running task found to cancel"}

    def _tool_check_task_status(self, task_id: str) -> dict:
        """Tool: check status of a specific task."""
        task = self.get_task(task_id)
        if task:
            status_code = task.get("cr_status")
            return {
                "task_id": task_id,
                "status": TASK_STATUS_NAMES.get(status_code, "Unknown"),
                "status_code": status_code,
                "name": task.get("cr_name", ""),
                "result": (task.get("cr_result") or "")[:500],
            }
        return {"error": f"Task {task_id} not found"}

    def _tool_list_recent_tasks(self) -> dict:
        """Tool: list recent tasks."""
        tasks = self.list_tasks(top=5)
        return {
            "tasks": [
                {
                    "task_id": t.get("cr_shraga_taskid", ""),
                    "name": t.get("cr_name", ""),
                    "status": TASK_STATUS_NAMES.get(t.get("cr_status"), "Unknown"),
                    "created": t.get("createdon", "")[:19],
                }
                for t in tasks
            ]
        }

    def _tool_provision_devbox(self) -> dict:
        """Tool: provision a new dev box for this user."""
        if not DEVBOX_AVAILABLE:
            return {"error": "Dev box provisioning module not available."}
        if not DEVCENTER_ENDPOINT or not DEVBOX_PROJECT:
            return {"error": "Dev box provisioning not configured."}
        if not USER_AZURE_AD_ID:
            return {"error": "Azure AD ID not configured."}

        try:
            devbox_mgr = DevBoxManager(
                devcenter_endpoint=DEVCENTER_ENDPOINT,
                project_name=DEVBOX_PROJECT,
                pool_name=DEVBOX_POOL,
                use_device_code=False,
            )
            result = devbox_mgr.provision_devbox(
                user_azure_ad_id=USER_AZURE_AD_ID,
                user_email=self.user_email,
            )
            devbox_name = result.get("name", "unknown")
            return {
                "success": True,
                "devbox_name": devbox_name,
                "provisioning_state": result.get("provisioningState", "Unknown"),
            }
        except Exception as e:
            return {"error": str(e)}

    def _execute_tool(self, tool_name: str, tool_args: dict,
                      msg_context: dict) -> dict:
        """Dispatch a tool call and return the result."""
        mcs_conv_id = msg_context.get("mcs_conv_id", "")
        inbound_row_id = msg_context.get("inbound_row_id", "")
        recent_tasks = msg_context.get("recent_tasks", [])

        if tool_name == "create_task":
            return self._tool_create_task(
                prompt=tool_args.get("prompt", ""),
                description=tool_args.get("description", ""),
                mcs_conversation_id=mcs_conv_id,
                inbound_row_id=inbound_row_id,
            )
        elif tool_name == "cancel_task":
            return self._tool_cancel_task(
                task_id=tool_args.get("task_id", ""),
                recent_tasks=recent_tasks,
            )
        elif tool_name == "check_task_status":
            return self._tool_check_task_status(
                task_id=tool_args.get("task_id", ""),
            )
        elif tool_name == "list_recent_tasks":
            return self._tool_list_recent_tasks()
        elif tool_name == "provision_devbox":
            return self._tool_provision_devbox()
        else:
            return {"error": f"Unknown tool: {tool_name}"}

    # ── Message Processing (Agentic) ──────────────────────────────────

    def process_message(self, msg: dict):
        """Process a single inbound message. Uses Claude with tools to understand
        intent and take action."""
        row_id = msg.get("cr_shraga_conversationid")
        mcs_conv_id = msg.get("cr_mcs_conversation_id", "")
        user_text = msg.get("cr_message", "").strip()

        if not user_text:
            self.mark_processed(row_id)
            return

        print(f"[MSG] Processing: {user_text[:80]}...")

        # Build context for Claude
        recent_tasks = self.list_tasks(top=5)
        context = self._build_context(recent_tasks)

        # Use Claude to decide what to do
        response_text, followup_expected = self._ask_claude(
            user_text, context, recent_tasks,
            mcs_conversation_id=mcs_conv_id, inbound_row_id=row_id,
        )

        # Send response back (with followup flag so the bot knows to wait for more)
        self.send_response(
            in_reply_to=row_id,
            mcs_conversation_id=mcs_conv_id,
            text=response_text,
            followup_expected=followup_expected,
        )

        # Mark inbound as processed
        self.mark_processed(row_id)
        print(f"[MSG] Responded: {response_text[:80]}...")

    def _build_context(self, recent_tasks: list[dict]) -> str:
        """Build context string about current state for Claude."""
        lines = [f"User: {self.user_email}"]

        if recent_tasks:
            lines.append("\nRecent tasks:")
            for t in recent_tasks:
                status = TASK_STATUS_NAMES.get(t.get("cr_status"), "Unknown")
                prompt = (t.get("cr_prompt") or "")[:100]
                task_id = t.get("cr_shraga_taskid", "")
                created = t.get("createdon", "")[:19]
                lines.append(f"  - [{status}] {prompt} (id: {task_id}, created: {created})")
        else:
            lines.append("\nNo recent tasks.")

        return "\n".join(lines)

    def _ask_claude(self, user_text: str, context: str, recent_tasks: list[dict],
                    mcs_conversation_id: str = "", inbound_row_id: str = "") -> tuple[str, bool]:
        """Use Claude CLI to process the user's message and return a response.

        Claude has tools available and decides what action to take.  The response
        format is JSON with either a tool_calls array or a final response.

        Returns (response_text, followup_expected).
        """
        tool_descriptions = (
            "Available tools (call via JSON):\n"
            "- create_task(prompt, description): Create a new coding task. "
            "Do NOT include file paths or working directories in the prompt -- "
            "the worker has its own session folder. Just describe what to create.\n"
            "- cancel_task(task_id): Cancel a task. Use 'latest' for the most recent running task.\n"
            "- check_task_status(task_id): Get current status of a specific task.\n"
            "- list_recent_tasks(): List the user's recent tasks.\n"
            "- provision_devbox(): Provision a new dev box for the user.\n"
        )

        system_prompt = (
            f"You are a task management assistant for a developer. You help them "
            f"create coding tasks, check task status, cancel tasks, and answer "
            f"questions about their work.\n\n"
            f"Current state:\n{context}\n\n"
            f"Dev box: {platform.node()}\n\n"
            f"{tool_descriptions}\n"
            f"Respond with JSON. Either:\n"
            f'  {{"response": "your message", "followup_expected": true/false}}\n'
            f"or:\n"
            f'  {{"tool_calls": [{{"name": "tool_name", "arguments": {{...}}}}]}}\n\n'
            f"After tool results come back, provide your final response.\n"
            f"Set followup_expected=true when you created a task (the monitor will "
            f"send a progress link later).\n\n"
            f"Keep responses concise and conversational. Be friendly but professional."
        )

        session_id = self._sessions.get(mcs_conversation_id) if mcs_conversation_id else None
        session_lost = False

        msg_context = {
            "mcs_conv_id": mcs_conversation_id,
            "inbound_row_id": inbound_row_id,
            "recent_tasks": recent_tasks,
        }

        try:
            response_text, new_session_id = self._call_claude(
                user_text, system_prompt, session_id=session_id,
            )

            # If resume failed, retry without resume
            if response_text is None and session_id:
                print(f"[SESSIONS] Resume failed for {session_id[:8]}..., starting fresh")
                self._forget_session(mcs_conversation_id)
                session_lost = True
                response_text, new_session_id = self._call_claude(
                    user_text, system_prompt, session_id=None,
                )

            if response_text is None:
                return FALLBACK_MESSAGE, False

            # Persist new session mapping
            if new_session_id and mcs_conversation_id:
                is_new = mcs_conversation_id not in self._sessions
                self._sessions[mcs_conversation_id] = new_session_id
                self._save_sessions()
                if is_new:
                    print(f"[SESSIONS] New session {new_session_id[:8]}... for {mcs_conversation_id[:20]}...")

            # Process Claude's response -- could be JSON with tools or plain text
            result, followup_expected = self._process_claude_response(
                response_text, msg_context, system_prompt,
            )

            # Prepend lost-session notice if applicable
            if session_lost:
                result = (
                    "[Note: I lost context from our previous conversation and started "
                    "a fresh session. Sorry about that!]\n\n" + result
                )

            return result, followup_expected

        except subprocess.TimeoutExpired:
            print("[WARN] Claude CLI timed out")
            return FALLBACK_MESSAGE, False
        except FileNotFoundError:
            print("[WARN] Claude CLI not found, using fallback")
            return FALLBACK_MESSAGE, False
        except Exception as e:
            print(f"[ERROR] _ask_claude: {e}")
            return FALLBACK_MESSAGE, False

    def _process_claude_response(self, response_text: str, msg_context: dict,
                                 system_prompt: str,
                                 max_rounds: int = 3) -> tuple[str, bool]:
        """Process Claude's response, executing tools if requested.

        Returns (final_text, followup_expected).
        """
        for _round in range(max_rounds):
            parsed = self._try_parse_json(response_text)

            if parsed is None:
                # Plain text response -- use as-is
                return response_text, False

            # Check for final response
            if "response" in parsed:
                followup = parsed.get("followup_expected", False)
                return parsed["response"], followup

            # Execute tool calls
            tool_calls = parsed.get("tool_calls", [])
            if not tool_calls:
                # No tools and no response -- use raw text
                return response_text, False

            tool_results = []
            created_task = False
            for tc in tool_calls:
                name = tc.get("name", "")
                args = tc.get("arguments", {})
                print(f"[TOOL] Executing: {name}({json.dumps(args)[:100]})")
                result = self._execute_tool(name, args, msg_context)
                tool_results.append({"tool": name, "result": result})
                print(f"[TOOL] Result: {json.dumps(result)[:200]}")
                if name == "create_task" and result.get("success"):
                    created_task = True

            # Feed results back to Claude for the next round
            followup_prompt = (
                f"Tool results:\n{json.dumps(tool_results, default=str)}\n\n"
                f"Provide your final response to the user as JSON:\n"
                f'  {{"response": "your message", "followup_expected": {str(created_task).lower()}}}'
            )

            next_response, _ = self._call_claude(
                followup_prompt, system_prompt, session_id=None,
            )
            if next_response is None:
                # Claude failed on follow-up -- construct a basic response
                if created_task:
                    return "Task submitted. I will send you a progress link once it starts running.", True
                return FALLBACK_MESSAGE, False

            response_text = next_response

        # Exhausted rounds -- return whatever we have
        return response_text, False

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

    def _call_claude(self, user_text: str, system_prompt: str,
                     session_id: str | None = None) -> tuple[str | None, str]:
        """Low-level Claude CLI call. Returns (response_text, session_id) or (None, '') on failure."""
        cmd = [
            "claude",
            "--print",
            "--output-format", "json",
            "--dangerously-skip-permissions",
            "--append-system-prompt", system_prompt,
        ]

        if CHAT_MODEL:
            cmd.extend(["--model", CHAT_MODEL])

        if session_id:
            cmd.extend(["--resume", session_id])

        cmd.append(user_text)

        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        cwd = self.working_dir if self.working_dir and os.path.isdir(self.working_dir) else None

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=90, env=env, cwd=cwd,
        )

        if result.returncode != 0:
            stderr = result.stderr[:300]
            print(f"[WARN] Claude CLI failed (rc={result.returncode}): {stderr}")
            # If this was a resume attempt, signal caller to retry without resume
            if session_id:
                return None, ""
            return None, ""

        raw = result.stdout.strip()
        if not raw:
            return None, ""

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Non-JSON response -- treat as plain text
            print(f"[WARN] Non-JSON response from Claude CLI")
            return raw, ""

        if data.get("is_error"):
            print(f"[WARN] Claude returned error: {data.get('result', '')[:200]}")
            if session_id:
                return None, ""
            return None, ""

        return data.get("result", ""), data.get("session_id", "")

    def _fallback_process(self, user_text: str, recent_tasks: list[dict]) -> str:
        """Minimal fallback when Claude CLI is completely unavailable."""
        return FALLBACK_MESSAGE

    # ── Main Loop ─────────────────────────────────────────────────────

    def run(self):
        """Main polling loop."""
        print(f"[START] Personal Task Manager for {self.user_email} | instance={INSTANCE_ID} | pid={os.getpid()}")
        print(f"[CONFIG] Dataverse: {DATAVERSE_URL}")
        print(f"[CONFIG] Poll interval: {POLL_INTERVAL}s")

        # Startup cleanup: mark stale unclaimed outbound rows as Delivered
        print("[STARTUP] Cleaning up stale outbound rows...")
        self.cleanup_stale_outbound()

        last_cleanup = time.time()

        while True:
            try:
                messages = self.poll_unclaimed()

                for msg in messages:
                    if self.claim_message(msg):
                        try:
                            self.process_message(msg)
                        except Exception as e:
                            row_id = msg.get("cr_shraga_conversationid", "?")
                            print(f"[ERROR] Processing message {row_id}: {e}")
                            # Try to send error response
                            try:
                                self.send_response(
                                    in_reply_to=row_id,
                                    mcs_conversation_id=msg.get("cr_mcs_conversation_id", ""),
                                    text=FALLBACK_MESSAGE,
                                )
                                self.mark_processed(row_id)
                            except Exception:
                                pass

                # Periodic stale row cleanup (every 30 minutes)
                if time.time() - last_cleanup > 1800:  # 30 minutes
                    self.cleanup_stale_outbound()
                    last_cleanup = time.time()

                time.sleep(POLL_INTERVAL)

            except KeyboardInterrupt:
                print("\n[STOP] Shutting down gracefully.")
                break
            except Exception as e:
                print(f"[ERROR] Main loop: {e}")
                time.sleep(POLL_INTERVAL * 2)


def main():
    user_email = USER_EMAIL
    if not user_email:
        print("ERROR: USER_EMAIL environment variable is required.")
        print("Usage: USER_EMAIL=you@company.com WORKING_DIR=/path/to/repo python task_manager.py")
        sys.exit(1)
    manager = TaskManager(user_email, working_dir=WORKING_DIR)
    manager.run()


if __name__ == "__main__":
    main()
