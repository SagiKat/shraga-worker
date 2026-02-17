"""
Personal Task Manager — Claude Code instance that handles one user's conversations.

Polls the conversations table for unclaimed inbound messages from its user,
processes them (create tasks, check status, answer questions), and writes
outbound responses. Runs on the user's dev box.
"""
import requests
import json
import time
import os
import sys
import subprocess
import platform
import threading
from pathlib import Path
from datetime import datetime, timezone, timedelta
from azure.identity import DefaultAzureCredential
from azure.core.credentials import AccessToken

# Import Dev Box manager (optional — only needed for PROVISION_DEVBOX action)
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
USER_EMAIL = os.environ.get("USER_EMAIL")  # Required — which user this manager serves
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "3"))  # seconds
REQUEST_TIMEOUT = 30
# Use a faster model for chat responses (latency-sensitive) while workers use the default model
CHAT_MODEL = os.environ.get("CHAT_MODEL", "")  # e.g., "claude-sonnet-4-5-20250929" for faster chat responses

# Conversation direction (string values in Dataverse)
DIRECTION_INBOUND = "Inbound"    # user → manager
DIRECTION_OUTBOUND = "Outbound"  # manager → user

# Conversation status (string values in Dataverse)
STATUS_UNCLAIMED = "Unclaimed"
STATUS_CLAIMED = "Claimed"
STATUS_PROCESSED = "Processed"
STATUS_DELIVERED = "Delivered"

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


WORKING_DIR = os.environ.get("WORKING_DIR", "")  # Dev box working directory for Claude


SESSIONS_FILE = os.environ.get("SESSIONS_FILE", "")  # Override path for session mapping

# Dev Box provisioning configuration (optional — set to enable PROVISION_DEVBOX action)
DEVCENTER_ENDPOINT = os.environ.get("DEVCENTER_ENDPOINT")
DEVBOX_PROJECT = os.environ.get("DEVBOX_PROJECT")
DEVBOX_POOL = os.environ.get("DEVBOX_POOL", "shraga-worker-pool")
USER_AZURE_AD_ID = os.environ.get("USER_AZURE_AD_ID", "")  # Azure AD object ID of the user


class TaskManager:
    """Personal Task Manager for a single user."""

    def __init__(self, user_email: str, working_dir: str = ""):
        if not user_email:
            raise ValueError("USER_EMAIL is required")
        self.user_email = user_email
        self.manager_id = f"personal:{user_email}"
        self.working_dir = working_dir or WORKING_DIR
        self.credential = DefaultAzureCredential()
        self._token_cache = None
        self._token_expires = None
        # MCS conversation ID → Claude session ID mapping (persisted to disk)
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
            return resp.json().get("value", [])
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
            print(f"[WARN] Cannot claim message — missing id or etag")
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
                "cr_claimed_by": self.manager_id,
            }
            resp = requests.patch(url, headers=headers, json=body, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 412:
                # Someone else claimed it first (optimistic concurrency conflict)
                print(f"[INFO] Message {row_id} already claimed by another manager")
                return False
            resp.raise_for_status()
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
        except Exception as e:
            print(f"[WARN] mark_processed failed: {e}")

    def send_response(self, in_reply_to: str, mcs_conversation_id: str, text: str,
                      followup_expected: bool = False):
        """Write an outbound response to the conversations table."""
        headers = self._headers(content_type="application/json")
        if not headers:
            print("[ERROR] Cannot send response — no auth token")
            return None
        try:
            body = {
                "cr_name": text[:200],
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
            if resp.status_code == 204:
                return {"cr_shraga_conversationid": "created"}
            return resp.json()
        except Exception as e:
            print(f"[ERROR] send_response: {e}")
            return None

    # ── Stale Row Cleanup ─────────────────────────────────────────────

    def cleanup_stale_outbound(self, max_age_minutes: int = 10):
        """Mark old Unclaimed Outbound rows as Delivered to prevent stale data issues.

        Per spec: old unclaimed Outbound rows from testing/crashes can interfere
        with the isFollowup filter. This marks them as Delivered so they're ignored.
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
                body = {"cr_status": STATUS_DELIVERED}
                resp = requests.patch(patch_url, headers=patch_headers, json=body, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                cleaned += 1
            except Exception as e:
                print(f"[CLEANUP] Error marking row {row_id} as Delivered: {e}")

        if cleaned > 0:
            print(f"[CLEANUP] Marked {cleaned} stale outbound row(s) as Delivered")
        return cleaned

    # ── Tasks CRUD ────────────────────────────────────────────────────

    def create_task(self, prompt: str, description: str = "") -> dict | None:
        """Create a new task in Dataverse. Returns the created row with task ID."""
        headers = self._headers(content_type="application/json")
        if not headers:
            return None
        try:
            body = {
                "cr_name": prompt[:200],  # Task title (primary name column, max 200 chars)
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
            print(f"[ERROR] create_task: {e}")
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
            deep_link = self.wait_for_running_link(task_id, timeout=60)

            # Build a situational prompt for Claude to compose the follow-up naturally
            if deep_link:
                situation = (
                    f"[SYSTEM UPDATE] The task '{task_title}' you just submitted is now running. "
                    f"The live progress link is: {deep_link}\n"
                    f"Send the user a natural follow-up message with this link. "
                    f"Keep it short and conversational."
                )
            else:
                situation = (
                    f"[SYSTEM UPDATE] The task '{task_title}' was submitted about a minute ago "
                    f"but hasn't started running yet. This could mean the worker is busy, "
                    f"there's a queue, or something went wrong.\n"
                    f"Send the user a natural follow-up. Keep the conversation going — "
                    f"suggest options like checking, retrying, or just waiting a bit longer."
                )

            # Call Claude with session context so the follow-up feels natural
            session_id = self._sessions.get(mcs_conversation_id)
            system_prompt = (
                "You are following up on a task you helped the user submit. "
                "Respond directly to the user — no action format needed, just your message."
            )

            followup_text, new_session_id = self._call_claude(
                situation, system_prompt, session_id=session_id,
            )

            # Update session if Claude returned a new one
            if new_session_id and mcs_conversation_id:
                self._sessions[mcs_conversation_id] = new_session_id
                self._save_sessions()

            # Strip ACTION format if Claude used it despite instructions
            if followup_text and "---" in followup_text:
                parts = followup_text.split("---", 1)
                if len(parts) > 1 and parts[1].strip():
                    followup_text = parts[1].strip()

            if not followup_text:
                # Fallback if Claude call fails
                if deep_link:
                    followup_text = f"Your task is running! Progress: {deep_link}"
                else:
                    followup_text = f"Haven't heard back on '{task_title}' yet. Want me to check?"

            # Send the follow-up as a proactive outbound message
            # Use inbound_row_id (not task_id) so the flow can match by strong ID
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

    # ── Message Processing ────────────────────────────────────────────

    def process_message(self, msg: dict):
        """Process a single inbound message. Uses Claude to understand intent and respond."""
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
                    mcs_conversation_id: str = "", inbound_row_id: str = "") -> str:
        """Use Claude CLI to process the user's message and return a response.

        Maintains conversation sessions per MCS conversation ID so Claude has
        full conversation history. Uses --output-format json to capture session IDs
        and --resume to continue existing sessions.

        Returns the response text to send back to the user.
        """
        system_prompt = f"""You are a task management assistant for a developer. You help them create coding tasks, check task status, cancel tasks, and answer questions about their work.

Current state:
{context}

Dev box: {platform.node()}
Working directory: {self.working_dir or 'not configured'}

IMPORTANT: You must respond with EXACTLY ONE of these action formats, followed by your message to the user.

ACTION FORMATS:

1. To create a task:
   ACTION: CREATE_TASK
   TITLE: <concise task title>
   DESCRIPTION: <detailed description of what to do>
   ---
   <your message confirming the task was submitted. Do NOT say "created" — say "submitted". A background process will monitor it and send the user a follow-up with the live progress link once the task starts running. So your message should be something like "Submitted! I'll send you the progress link once it starts running.">

2. To cancel a task (cancel the most recent running task, or a specific one):
   ACTION: CANCEL_TASK
   TASK_ID: <task id to cancel, or "latest" for the most recent running task>
   ---
   <your message to the user confirming cancellation>

3. To just respond (status check, questions, general chat):
   ACTION: RESPOND
   ---
   <your response to the user>

4. To provision a new dev box (when user asks for a new dev box, additional dev box, etc.):
   ACTION: PROVISION_DEVBOX
   ---
   <your message to the user acknowledging the request>

Keep responses concise and conversational. Be friendly but professional."""

        session_id = self._sessions.get(mcs_conversation_id) if mcs_conversation_id else None
        session_lost = False

        try:
            response_text, new_session_id = self._call_claude(
                user_text, system_prompt, session_id=session_id,
            )

            # If resume failed, the call returns None — retry without resume
            if response_text is None and session_id:
                print(f"[SESSIONS] Resume failed for {session_id[:8]}..., starting fresh")
                self._forget_session(mcs_conversation_id)
                session_lost = True
                response_text, new_session_id = self._call_claude(
                    user_text, system_prompt, session_id=None,
                )

            if response_text is None:
                return self._fallback_process(user_text, recent_tasks), False

            # Persist new session mapping
            if new_session_id and mcs_conversation_id:
                is_new = mcs_conversation_id not in self._sessions
                self._sessions[mcs_conversation_id] = new_session_id
                self._save_sessions()
                if is_new:
                    print(f"[SESSIONS] New session {new_session_id[:8]}... for {mcs_conversation_id[:20]}...")

            # Execute the action from Claude's response
            result, followup_expected = self._execute_action(
                response_text, recent_tasks,
                mcs_conversation_id=mcs_conversation_id,
                inbound_row_id=inbound_row_id,
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
            return self._fallback_process(user_text, recent_tasks), False
        except FileNotFoundError:
            print("[WARN] Claude CLI not found, using fallback")
            return self._fallback_process(user_text, recent_tasks), False
        except Exception as e:
            print(f"[ERROR] _ask_claude: {e}")
            return self._fallback_process(user_text, recent_tasks), False

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
            # Non-JSON response — treat as plain text
            print(f"[WARN] Non-JSON response from Claude CLI")
            return raw, ""

        if data.get("is_error"):
            print(f"[WARN] Claude returned error: {data.get('result', '')[:200]}")
            if session_id:
                return None, ""
            return None, ""

        return data.get("result", ""), data.get("session_id", "")

    def _execute_action(self, claude_response: str, recent_tasks: list[dict],
                        mcs_conversation_id: str = "",
                        inbound_row_id: str = "") -> tuple[str, bool]:
        """Parse Claude's action response and execute it.
        Returns (response_text, followup_expected)."""
        lines = claude_response.split("\n")

        action = None
        title = None
        description = None
        task_id = None
        message_lines = []
        in_message = False

        for line in lines:
            stripped = line.strip()
            if stripped == "---":
                in_message = True
                continue
            if in_message:
                message_lines.append(line)
                continue
            if stripped.startswith("ACTION:"):
                action = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("TITLE:"):
                title = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("DESCRIPTION:"):
                description = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("TASK_ID:"):
                task_id = stripped.split(":", 1)[1].strip()

        user_message = "\n".join(message_lines).strip()

        if action == "CREATE_TASK" and title:
            task = self.create_task(prompt=title, description=description or "")
            if task:
                new_task_id = task.get("cr_shraga_taskid", "")

                # Kick off background monitoring for the Running card
                if new_task_id and mcs_conversation_id:
                    t = threading.Thread(
                        target=self._monitor_task_start,
                        args=(new_task_id, title, mcs_conversation_id, inbound_row_id),
                        daemon=True,
                    )
                    t.start()
                    print(f"[MONITOR] Background thread started for task {new_task_id[:8]}...")

                return user_message, True  # followup expected
            else:
                return user_message, False

        elif action == "CANCEL_TASK":
            target_id = task_id
            if target_id == "latest" or not target_id:
                for t in recent_tasks:
                    if t.get("cr_status") == TASK_RUNNING:
                        target_id = t.get("cr_shraga_taskid")
                        break
            if target_id and target_id != "latest":
                self.cancel_task(target_id)
            return user_message, False

        elif action == "PROVISION_DEVBOX":
            result = self._handle_provision_devbox()
            # If handler returned a message, use it; otherwise fall back to Claude's message
            return (result or user_message or claude_response), False

        else:
            # RESPOND or unparseable — just pass through Claude's message
            return (user_message or claude_response), False

    def _handle_provision_devbox(self) -> str:
        """Handle the PROVISION_DEVBOX action.

        Checks if DevBoxManager is configured, then provisions a new dev box
        for the current user. Returns a status message string.
        """
        if not DEVBOX_AVAILABLE:
            print("[DEVBOX] orchestrator_devbox module not available")
            return "Dev box provisioning is not configured on this instance. Please contact your admin."

        if not DEVCENTER_ENDPOINT or not DEVBOX_PROJECT:
            print("[DEVBOX] Missing DEVCENTER_ENDPOINT or DEVBOX_PROJECT env vars")
            return "Dev box provisioning is not configured. Please contact your admin to set up the required environment variables."

        if not USER_AZURE_AD_ID:
            print("[DEVBOX] Missing USER_AZURE_AD_ID env var")
            return "Dev box provisioning requires your Azure AD ID to be configured. Please contact your admin."

        try:
            devbox_mgr = DevBoxManager(
                devcenter_endpoint=DEVCENTER_ENDPOINT,
                project_name=DEVBOX_PROJECT,
                pool_name=DEVBOX_POOL,
                use_device_code=False,
            )
            print(f"[DEVBOX] Starting provisioning for {self.user_email}...")

            result = devbox_mgr.provision_devbox(
                user_azure_ad_id=USER_AZURE_AD_ID,
                user_email=self.user_email,
            )

            devbox_name = result.get("name", "unknown")
            provisioning_state = result.get("provisioningState", "Unknown")
            print(f"[DEVBOX] Provisioning started: {devbox_name} (state: {provisioning_state})")

            return (
                f"Dev box provisioning has been started. "
                f"Box name: **{devbox_name}** (current state: {provisioning_state}). "
                f"This usually takes a few minutes. I'll let you know once it's ready, "
                f"or you can ask me for a status update."
            )

        except Exception as e:
            print(f"[ERROR] _handle_provision_devbox: {e}")
            return f"Something went wrong while provisioning the dev box: {e}"

    def _fallback_process(self, user_text: str, recent_tasks: list[dict]) -> str:
        """Minimal fallback when Claude CLI is completely unavailable."""
        return "The worker on your dev box is not responding right now. It may need to be restarted. Please try again shortly or contact support if this persists."

    # ── Main Loop ─────────────────────────────────────────────────────

    def run(self):
        """Main polling loop."""
        print(f"[START] Personal Task Manager for {self.user_email}")
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
                                    text="Something went wrong on my end. Please try again.",
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
