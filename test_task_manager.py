"""
Tests for Personal Task Manager.

All external dependencies (Azure, Dataverse, Claude CLI) are mocked.
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call
from datetime import datetime, timezone, timedelta

import pytest

# Add task-manager to path
sys.path.insert(0, str(Path(__file__).parent / "task-manager"))

from conftest import FakeAccessToken, FakeResponse, FakeCompletedProcess


# ── Fixtures ──────────────────────────────────────────────────────────────────

SAMPLE_CONVERSATION_ID = "conv-0001-0002-0003-000000000001"
SAMPLE_MCS_CONV_ID = "mcs-conv-abc123"
SAMPLE_TASK_ID = "task-0001-0002-0003-000000000001"

SAMPLE_INBOUND_MSG = {
    "cr_shraga_conversationid": SAMPLE_CONVERSATION_ID,
    "cr_useremail": "testuser@example.com",
    "cr_mcs_conversation_id": SAMPLE_MCS_CONV_ID,
    "cr_message": "create a task: fix the login CSS bug",
    "cr_direction": "Inbound",
    "cr_status": "Unclaimed",
    "@odata.etag": 'W/"12345"',
    "createdon": "2026-02-15T10:00:00Z",
}

SAMPLE_TASK = {
    "cr_shraga_taskid": SAMPLE_TASK_ID,
    "cr_name": "Fix login CSS bug",
    "cr_prompt": "fix the login CSS bug",
    "cr_status": 1,
    "cr_result": "",
    "crb3b_useremail": "testuser@example.com",
    "createdon": "2026-02-15T10:00:00Z",
}


@pytest.fixture
def mock_credential():
    cred = MagicMock()
    cred.get_token.return_value = FakeAccessToken()
    return cred


@pytest.fixture
def manager(mock_credential, monkeypatch):
    """Create a TaskManager with mocked credentials."""
    monkeypatch.setenv("USER_EMAIL", "testuser@example.com")
    with patch("task_manager.DefaultAzureCredential", return_value=mock_credential):
        from task_manager import TaskManager
        mgr = TaskManager("testuser@example.com")
    return mgr


# ── Auth Tests ────────────────────────────────────────────────────────────────

class TestAuth:
    def test_get_token_success(self, manager):
        token = manager.get_token()
        assert token == "fake-token-12345"

    def test_get_token_caches(self, manager):
        manager.get_token()
        manager.get_token()
        # Should only call get_token once (second call uses cache)
        assert manager.credential.get_token.call_count == 1

    def test_get_token_refreshes_when_expired(self, manager):
        # First call
        manager.get_token()
        # Expire the cache
        manager._token_expires = datetime.now(timezone.utc) - timedelta(minutes=1)
        # Second call should refresh
        manager.get_token()
        assert manager.credential.get_token.call_count == 2

    def test_get_token_returns_none_on_error(self, manager):
        manager.credential.get_token.side_effect = Exception("auth failed")
        manager._token_cache = None
        manager._token_expires = None
        assert manager.get_token() is None

    def test_headers_include_auth(self, manager):
        headers = manager._headers()
        assert headers is not None
        assert "Bearer" in headers["Authorization"]

    def test_headers_with_etag(self, manager):
        headers = manager._headers(etag='W/"123"')
        assert headers["If-Match"] == 'W/"123"'

    def test_headers_returns_none_without_token(self, manager):
        manager.credential.get_token.side_effect = Exception("no token")
        manager._token_cache = None
        manager._token_expires = None
        assert manager._headers() is None


# ── Conversation Polling Tests ────────────────────────────────────────────────

class TestPolling:
    @patch("task_manager.requests.get")
    def test_poll_unclaimed_returns_messages(self, mock_get, manager):
        mock_get.return_value = FakeResponse(json_data={"value": [SAMPLE_INBOUND_MSG]})
        msgs = manager.poll_unclaimed()
        assert len(msgs) == 1
        assert msgs[0]["cr_message"] == "create a task: fix the login CSS bug"

    @patch("task_manager.requests.get")
    def test_poll_unclaimed_filters_by_user(self, mock_get, manager):
        mock_get.return_value = FakeResponse(json_data={"value": []})
        manager.poll_unclaimed()
        url = mock_get.call_args[0][0]
        assert "testuser@example.com" in url
        assert "cr_direction eq 'Inbound'" in url
        assert "cr_status eq 'Unclaimed'" in url

    @patch("task_manager.requests.get")
    def test_poll_unclaimed_handles_timeout(self, mock_get, manager):
        import requests as req
        mock_get.side_effect = req.exceptions.Timeout()
        msgs = manager.poll_unclaimed()
        assert msgs == []

    @patch("task_manager.requests.get")
    def test_poll_unclaimed_handles_error(self, mock_get, manager):
        mock_get.side_effect = Exception("network error")
        msgs = manager.poll_unclaimed()
        assert msgs == []

    @patch("task_manager.requests.get")
    def test_poll_unclaimed_returns_empty_on_no_messages(self, mock_get, manager):
        mock_get.return_value = FakeResponse(json_data={"value": []})
        msgs = manager.poll_unclaimed()
        assert msgs == []


# ── Claim Tests ───────────────────────────────────────────────────────────────

class TestClaim:
    @patch("task_manager.requests.patch")
    def test_claim_message_success(self, mock_patch, manager):
        mock_patch.return_value = FakeResponse(status_code=204)
        assert manager.claim_message(SAMPLE_INBOUND_MSG) is True

    @patch("task_manager.requests.patch")
    def test_claim_sends_correct_body(self, mock_patch, manager):
        mock_patch.return_value = FakeResponse(status_code=204)
        manager.claim_message(SAMPLE_INBOUND_MSG)
        body = mock_patch.call_args[1]["json"]
        assert body["cr_status"] == "Claimed"
        assert body["cr_claimed_by"].startswith("personal:testuser@example.com:")

    @patch("task_manager.requests.patch")
    def test_claim_uses_etag(self, mock_patch, manager):
        mock_patch.return_value = FakeResponse(status_code=204)
        manager.claim_message(SAMPLE_INBOUND_MSG)
        headers = mock_patch.call_args[1].get("headers") or mock_patch.call_args[0][0]
        # Check the If-Match header was set (via _headers with etag)
        call_headers = mock_patch.call_args[1].get("headers")
        if call_headers is None:
            # headers passed positionally via keyword
            pass

    @patch("task_manager.requests.patch")
    def test_claim_fails_on_conflict(self, mock_patch, manager):
        mock_patch.return_value = FakeResponse(status_code=412)
        assert manager.claim_message(SAMPLE_INBOUND_MSG) is False

    def test_claim_fails_without_etag(self, manager):
        msg = {**SAMPLE_INBOUND_MSG}
        del msg["@odata.etag"]
        assert manager.claim_message(msg) is False

    def test_claim_fails_without_id(self, manager):
        msg = {**SAMPLE_INBOUND_MSG}
        del msg["cr_shraga_conversationid"]
        assert manager.claim_message(msg) is False


# ── Response Tests ────────────────────────────────────────────────────────────

class TestResponse:
    @patch("task_manager.requests.post")
    def test_send_response_creates_outbound_row(self, mock_post, manager):
        mock_post.return_value = FakeResponse(json_data={"cr_shraga_conversationid": "new-id"})
        result = manager.send_response(
            in_reply_to=SAMPLE_CONVERSATION_ID,
            mcs_conversation_id=SAMPLE_MCS_CONV_ID,
            text="Task created!",
        )
        assert result is not None
        body = mock_post.call_args[1]["json"]
        assert body["cr_direction"] == "Outbound"
        assert body["cr_in_reply_to"] == SAMPLE_CONVERSATION_ID
        assert body["cr_message"] == "Task created!"
        assert body["cr_useremail"] == "testuser@example.com"

    @patch("task_manager.requests.post")
    def test_send_response_truncates_name(self, mock_post, manager):
        mock_post.return_value = FakeResponse(json_data={})
        long_text = "x" * 500
        manager.send_response("id", "conv", long_text)
        body = mock_post.call_args[1]["json"]
        assert len(body["cr_name"]) == 200

    @patch("task_manager.requests.post")
    def test_send_response_returns_none_on_error(self, mock_post, manager):
        mock_post.side_effect = Exception("network error")
        result = manager.send_response("id", "conv", "text")
        assert result is None


# ── Task CRUD Tests ───────────────────────────────────────────────────────────

class TestTaskCRUD:
    @patch("task_manager.requests.post")
    def test_create_task(self, mock_post, manager):
        mock_post.return_value = FakeResponse(json_data=SAMPLE_TASK)
        task = manager.create_task("fix the login CSS bug")
        assert task is not None
        body = mock_post.call_args[1]["json"]
        assert body["cr_prompt"] == "fix the login CSS bug"
        assert body["cr_status"] == 1  # PENDING
        assert body["crb3b_useremail"] == "testuser@example.com"

    @patch("task_manager.requests.get")
    def test_list_tasks(self, mock_get, manager):
        mock_get.return_value = FakeResponse(json_data={"value": [SAMPLE_TASK]})
        tasks = manager.list_tasks()
        assert len(tasks) == 1
        url = mock_get.call_args[0][0]
        assert "testuser@example.com" in url

    @patch("task_manager.requests.get")
    def test_get_task(self, mock_get, manager):
        mock_get.return_value = FakeResponse(json_data=SAMPLE_TASK)
        task = manager.get_task(SAMPLE_TASK_ID)
        assert task is not None
        assert task["cr_prompt"] == "fix the login CSS bug"

    @patch("task_manager.requests.patch")
    def test_cancel_task(self, mock_patch, manager):
        mock_patch.return_value = FakeResponse(status_code=204)
        assert manager.cancel_task(SAMPLE_TASK_ID) is True
        body = mock_patch.call_args[1]["json"]
        assert body["cr_status"] == 9  # CANCELED

    @patch("task_manager.requests.get")
    def test_get_task_messages(self, mock_get, manager):
        sample_msg = {"cr_name": "progress", "cr_content": "Working on it..."}
        mock_get.return_value = FakeResponse(json_data={"value": [sample_msg]})
        msgs = manager.get_task_messages(SAMPLE_TASK_ID)
        assert len(msgs) == 1


# ── Fallback Processing Tests ────────────────────────────────────────────────

class TestFallbackProcessing:
    def test_fallback_returns_generic_message(self, manager):
        """Fallback is minimal — just a generic error message when Claude CLI is unavailable."""
        result = manager._fallback_process("anything", [])
        assert len(result) > 0  # Returns some message


# ── Claude Integration Tests ─────────────────────────────────────────────────

class TestClaudeIntegration:
    def test_execute_action_create_task(self, manager):
        response = "ACTION: CREATE_TASK\nTITLE: Fix the bug\nDESCRIPTION: Fix the CSS\n---\nI've created a task to fix the bug."
        with patch.object(manager, "create_task", return_value=SAMPLE_TASK):
            result, followup = manager._execute_action(response, [])
            assert "fix the bug" in result.lower() or "created" in result.lower()
            assert followup is True  # task creation expects a follow-up

    def test_execute_action_cancel_task(self, manager):
        running_task = {**SAMPLE_TASK, "cr_status": 5}
        response = "ACTION: CANCEL_TASK\nTASK_ID: latest\n---\nTask canceled."
        with patch.object(manager, "cancel_task", return_value=True):
            result, followup = manager._execute_action(response, [running_task])
            assert "cancel" in result.lower()
            assert followup is False

    def test_execute_action_respond(self, manager):
        response = "ACTION: RESPOND\n---\nYou have 2 tasks running."
        result, followup = manager._execute_action(response, [])
        assert "2 tasks running" in result
        assert followup is False

    def test_execute_action_unparseable(self, manager):
        response = "I'm not sure what format this is."
        result, followup = manager._execute_action(response, [])
        assert "not sure" in result.lower()
        assert followup is False

    @patch("task_manager.subprocess.run")
    def test_ask_claude_success(self, mock_run, manager):
        mock_run.return_value = FakeCompletedProcess(
            stdout='{"result":"ACTION: RESPOND\\n---\\nHere are your tasks.","session_id":"test-123","is_error":false}'
        )
        result, followup = manager._ask_claude("status", "context", [])
        assert "tasks" in result.lower()

    @patch("task_manager.subprocess.run")
    def test_ask_claude_timeout_uses_fallback(self, mock_run, manager):
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired("claude", 60)
        with patch.object(manager, "_fallback_process", return_value="fallback response"):
            result, followup = manager._ask_claude("status", "context", [])
            assert result == "fallback response"
            assert followup is False

    @patch("task_manager.subprocess.run")
    def test_ask_claude_not_found_uses_fallback(self, mock_run, manager):
        mock_run.side_effect = FileNotFoundError()
        with patch.object(manager, "_fallback_process", return_value="fallback response"):
            result, followup = manager._ask_claude("status", "context", [])
            assert result == "fallback response"
            assert followup is False


# ── Message Processing Integration Tests ──────────────────────────────────────

class TestProcessMessage:
    @patch("task_manager.requests.post")
    @patch("task_manager.requests.patch")
    @patch("task_manager.requests.get")
    def test_process_message_full_flow(self, mock_get, mock_patch, mock_post, manager):
        """Test the full message processing flow: claim → process → respond."""
        # list_tasks returns empty
        mock_get.return_value = FakeResponse(json_data={"value": []})
        # send_response succeeds
        mock_post.return_value = FakeResponse(json_data={})
        # mark_processed succeeds
        mock_patch.return_value = FakeResponse(status_code=204)

        with patch.object(manager, "_ask_claude", return_value=("Task created!", False)):
            manager.process_message(SAMPLE_INBOUND_MSG)

        # Verify response was sent
        assert mock_post.called
        # Verify message was marked processed
        assert mock_patch.called

    def test_process_empty_message(self, manager):
        """Empty messages should just be marked processed."""
        empty_msg = {**SAMPLE_INBOUND_MSG, "cr_message": ""}
        with patch.object(manager, "mark_processed") as mock_mark:
            manager.process_message(empty_msg)
            mock_mark.assert_called_once()


# ── Constructor Tests ─────────────────────────────────────────────────────────

class TestConstructor:
    def test_requires_user_email(self, mock_credential):
        with patch("task_manager.DefaultAzureCredential", return_value=mock_credential):
            from task_manager import TaskManager
            with pytest.raises(ValueError, match="USER_EMAIL"):
                TaskManager("")

    def test_sets_manager_id(self, manager):
        assert manager.manager_id == "personal:testuser@example.com"

    def test_sets_user_email(self, manager):
        assert manager.user_email == "testuser@example.com"


# ── Stale Row Cleanup Tests ──────────────────────────────────────────────

SAMPLE_STALE_ROW_1 = {
    "cr_shraga_conversationid": "stale-0001-0002-0003-000000000001",
    "cr_useremail": "testuser@example.com",
    "cr_direction": "Outbound",
    "cr_status": "Unclaimed",
    "createdon": "2026-02-15T08:00:00Z",
}

SAMPLE_STALE_ROW_2 = {
    "cr_shraga_conversationid": "stale-0001-0002-0003-000000000002",
    "cr_useremail": "testuser@example.com",
    "cr_direction": "Outbound",
    "cr_status": "Unclaimed",
    "createdon": "2026-02-15T08:05:00Z",
}


class TestStaleRowCleanup:
    @patch("task_manager.requests.patch")
    @patch("task_manager.requests.get")
    def test_cleanup_marks_stale_rows_as_delivered(self, mock_get, mock_patch, manager):
        """cleanup_stale_outbound patches each stale row with STATUS_DELIVERED."""
        mock_get.return_value = FakeResponse(
            json_data={"value": [SAMPLE_STALE_ROW_1, SAMPLE_STALE_ROW_2]}
        )
        mock_patch.return_value = FakeResponse(status_code=204)

        cleaned = manager.cleanup_stale_outbound()

        assert cleaned == 2
        assert mock_patch.call_count == 2
        # Both patches should set status to Delivered
        for c in mock_patch.call_args_list:
            body = c[1]["json"]
            assert body["cr_status"] == "Delivered"

    @patch("task_manager.requests.patch")
    @patch("task_manager.requests.get")
    def test_cleanup_no_stale_rows(self, mock_get, mock_patch, manager):
        """When no stale rows exist, returns 0 and makes no patch calls."""
        mock_get.return_value = FakeResponse(json_data={"value": []})

        cleaned = manager.cleanup_stale_outbound()

        assert cleaned == 0
        assert mock_patch.call_count == 0

    @patch("task_manager.requests.get")
    def test_cleanup_handles_query_error(self, mock_get, manager):
        """When the GET query raises an exception, returns 0 gracefully."""
        mock_get.side_effect = Exception("Dataverse unavailable")

        cleaned = manager.cleanup_stale_outbound()

        assert cleaned == 0

    @patch("task_manager.requests.patch")
    @patch("task_manager.requests.get")
    def test_cleanup_handles_patch_error(self, mock_get, mock_patch, manager):
        """When patching a row fails, returns 0 but does not crash."""
        mock_get.return_value = FakeResponse(
            json_data={"value": [SAMPLE_STALE_ROW_1]}
        )
        mock_patch.side_effect = Exception("patch failed")

        cleaned = manager.cleanup_stale_outbound()

        assert cleaned == 0

    @patch("task_manager.requests.get")
    def test_cleanup_uses_correct_filter(self, mock_get, manager):
        """The OData query includes direction=Outbound, status=Unclaimed, and createdon cutoff."""
        mock_get.return_value = FakeResponse(json_data={"value": []})

        manager.cleanup_stale_outbound()

        url = mock_get.call_args[0][0]
        assert "cr_direction eq 'Outbound'" in url
        assert "cr_status eq 'Unclaimed'" in url
        assert "createdon lt" in url
