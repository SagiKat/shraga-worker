"""Tests for integrated_task_worker.py – IntegratedTaskWorker"""
import json
import os
import sys
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock, call
from datetime import datetime, timezone, timedelta

# We need to patch azure.identity and the WEBHOOK_URL check BEFORE importing
# the module, because it runs at import time.


def _import_worker(monkeypatch, tmp_path):
    """Helper: import the worker module with all necessary patches."""
    monkeypatch.setenv("DATAVERSE_URL", "https://test-org.crm.dynamics.com")
    monkeypatch.setenv("TABLE_NAME", "cr_shraga_tasks")
    monkeypatch.setenv("WEBHOOK_USER", "testuser@example.com")

    # Remove cached module to force re-import with new env vars
    for mod_name in list(sys.modules):
        if mod_name == "integrated_task_worker":
            del sys.modules[mod_name]

    # Mock the AgentCLI import that happens at module level
    mock_agent_module = MagicMock()
    monkeypatch.setitem(sys.modules, "autonomous_agent", mock_agent_module)

    # Patch DefaultAzureCredential before import
    with patch("azure.identity.DefaultAzureCredential") as mock_cred:
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(
            token="fake-token",
            expires_on=(datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()
        )
        mock_cred.return_value = mock_cred_inst

        import integrated_task_worker as mod
        return mod, mock_cred_inst


# ===========================================================================
# Token management
# ===========================================================================

class TestGetToken:

    def test_get_token_returns_token(self, monkeypatch, tmp_path):
        mod, mock_cred = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        token = worker.get_token()
        assert token == "fake-token"

    def test_get_token_caches(self, monkeypatch, tmp_path):
        mod, mock_cred = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        t1 = worker.get_token()
        t2 = worker.get_token()
        # Should only call get_token once due to caching
        assert mock_cred.get_token.call_count == 1
        assert t1 == t2

    def test_get_token_refreshes_when_expired(self, monkeypatch, tmp_path):
        mod, mock_cred = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        worker.get_token()
        # Force expire
        worker._token_expires = datetime.now(timezone.utc) - timedelta(hours=1)
        worker.get_token()
        assert mock_cred.get_token.call_count == 2

    def test_get_token_returns_none_on_error(self, monkeypatch, tmp_path):
        mod, mock_cred = _import_worker(monkeypatch, tmp_path)
        mock_cred.get_token.side_effect = Exception("Auth failed")
        worker = mod.IntegratedTaskWorker()
        # Reset cache
        worker._token_cache = None
        worker._token_expires = None
        token = worker.get_token()
        assert token is None


# ===========================================================================
# State management
# ===========================================================================

class TestStateManagement:

    def test_save_and_load_state(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        worker.current_user_id = "test-user-123"
        worker.save_state()

        worker2 = mod.IntegratedTaskWorker()
        assert worker2.current_user_id == "test-user-123"

    def test_load_state_no_file(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        assert worker.current_user_id is None


# ===========================================================================
# Version management
# ===========================================================================

class TestVersionManagement:

    def test_load_version_from_file(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        # Create VERSION file where repo_path points
        worker = mod.IntegratedTaskWorker()
        (worker.repo_path / "VERSION").write_text("2.0.0")
        version = worker.load_version()
        assert version == "2.0.0"

    def test_load_version_returns_unknown_if_missing(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        # Ensure VERSION file doesn't exist in tmp_path
        vf = worker.repo_path / "VERSION"
        if vf.exists():
            vf.unlink()
        version = worker.load_version()
        assert version == "unknown"


# ===========================================================================
# get_current_user
# ===========================================================================

class TestGetCurrentUser:

    @patch("integrated_task_worker.requests.get")
    def test_get_current_user_success(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"UserId": "user-abc-123", "BusinessUnitId": "bu1"},
            raise_for_status=MagicMock()
        )
        worker = mod.IntegratedTaskWorker()
        uid = worker.get_current_user()
        assert uid == "user-abc-123"
        assert worker.current_user_id == "user-abc-123"

    @patch("integrated_task_worker.requests.get")
    def test_get_current_user_failure(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.side_effect = Exception("Network error")
        worker = mod.IntegratedTaskWorker()
        uid = worker.get_current_user()
        assert uid is None


# ===========================================================================
# check_for_updates
# ===========================================================================

class TestCheckForUpdates:

    @patch("integrated_task_worker.subprocess.run")
    def test_no_update_when_versions_match(self, mock_run, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        worker.current_version = "1.0.0"

        # git fetch succeeds, git show returns same version
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="1.0.0\n", stderr=""),
        ]

        assert worker.check_for_updates() is False

    @patch("integrated_task_worker.subprocess.run")
    def test_update_available_when_versions_differ(self, mock_run, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        worker.current_version = "1.0.0"

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="1.1.0\n", stderr=""),
        ]

        assert worker.check_for_updates() is True

    @patch("integrated_task_worker.subprocess.run")
    def test_returns_false_on_fetch_failure(self, mock_run, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        mock_run.return_value = MagicMock(returncode=1, stderr="fetch failed")
        assert worker.check_for_updates() is False


# ===========================================================================
# append_to_transcript
# ===========================================================================

class TestAppendToTranscript:

    def test_append_to_empty_transcript(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        result = worker.append_to_transcript("", "system", "Hello")
        parsed = json.loads(result)
        assert parsed["from"] == "system"
        assert parsed["message"] == "Hello"
        assert "time" in parsed

    def test_append_to_existing_transcript(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        existing = json.dumps({"from": "worker", "time": "2026-01-01T00:00:00", "message": "First"})
        result = worker.append_to_transcript(existing, "system", "Second")
        lines = result.strip().split("\n")
        assert len(lines) == 2
        last = json.loads(lines[1])
        assert last["message"] == "Second"


# ===========================================================================
# update_task
# ===========================================================================

class TestUpdateTask:

    @patch("integrated_task_worker.requests.patch")
    def test_update_task_success(self, mock_patch, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_patch.return_value = MagicMock(raise_for_status=MagicMock())
        worker = mod.IntegratedTaskWorker()
        result = worker.update_task("task-123", status=5, status_message="Running")
        assert result is True
        # Verify PATCH was called with correct data
        call_kwargs = mock_patch.call_args
        sent_data = call_kwargs[1]["json"]
        assert sent_data["cr_status"] == 5
        assert sent_data["cr_statusmessage"] == "Running"

    @patch("integrated_task_worker.requests.patch")
    def test_update_task_failure(self, mock_patch, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_patch.side_effect = Exception("Network error")
        worker = mod.IntegratedTaskWorker()
        result = worker.update_task("task-123", status=5)
        assert result is False

    @patch("integrated_task_worker.requests.patch")
    def test_update_task_skips_none_values(self, mock_patch, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_patch.return_value = MagicMock(raise_for_status=MagicMock())
        worker = mod.IntegratedTaskWorker()
        worker.update_task("task-123", status=7, status_message=None)
        sent_data = mock_patch.call_args[1]["json"]
        assert "cr_status" in sent_data
        # status_message is None so should not be in payload
        # Actually the code does include None values only if they're not None
        # Let's check the code logic: if status_message is not None: data["..."] = ...
        assert "cr_statusmessage" not in sent_data


# ===========================================================================
# send_to_webhook
# ===========================================================================

class TestSendToWebhook:

    @patch("integrated_task_worker.requests.post")
    def test_send_success(self, mock_post, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_post.return_value = MagicMock(raise_for_status=MagicMock())
        worker = mod.IntegratedTaskWorker()
        result = worker.send_to_webhook("Test message")
        assert result is True

    @patch("integrated_task_worker.requests.post")
    def test_send_truncates_title(self, mock_post, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_post.return_value = MagicMock(raise_for_status=MagicMock())
        worker = mod.IntegratedTaskWorker()
        long_msg = "A" * 500
        worker.send_to_webhook(long_msg)
        sent_data = mock_post.call_args[1]["json"]
        assert len(sent_data["cr_name"]) <= 450

    @patch("integrated_task_worker.requests.post")
    def test_send_includes_task_id_when_set(self, mock_post, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_post.return_value = MagicMock(raise_for_status=MagicMock())
        worker = mod.IntegratedTaskWorker()
        worker.current_task_id = "task-abc-123"
        worker.send_to_webhook("Test message")
        sent_data = mock_post.call_args[1]["json"]
        assert sent_data["crb3b_taskid"] == "task-abc-123"

    @patch("integrated_task_worker.requests.post")
    def test_send_omits_task_id_when_none(self, mock_post, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_post.return_value = MagicMock(raise_for_status=MagicMock())
        worker = mod.IntegratedTaskWorker()
        worker.current_task_id = None
        worker.send_to_webhook("Test message")
        sent_data = mock_post.call_args[1]["json"]
        assert "crb3b_taskid" not in sent_data

    @patch("integrated_task_worker.requests.post")
    def test_send_retries_with_truncation_on_400_large_message(self, mock_post, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        import requests as req_lib
        # First call fails with 400, second succeeds
        error_response = MagicMock()
        error_response.status_code = 400
        error_response.text = "Request too large"
        first_error = req_lib.exceptions.HTTPError(response=error_response)
        mock_post.side_effect = [first_error, MagicMock(raise_for_status=MagicMock())]

        worker = mod.IntegratedTaskWorker()
        large_msg = "X" * 20000
        result = worker.send_to_webhook(large_msg)
        assert result is True
        assert mock_post.call_count == 2
        # Second call should have truncated content
        retry_data = mock_post.call_args_list[1][1]["json"]
        assert len(retry_data["cr_content"]) < 20000

    @patch("integrated_task_worker.requests.post")
    def test_send_no_retry_on_400_small_message(self, mock_post, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        import requests as req_lib
        error_response = MagicMock()
        error_response.status_code = 400
        error_response.text = "Bad request"
        first_error = req_lib.exceptions.HTTPError(response=error_response)
        mock_post.side_effect = first_error

        worker = mod.IntegratedTaskWorker()
        result = worker.send_to_webhook("Short message")
        assert result is False
        assert mock_post.call_count == 1

    @patch("integrated_task_worker.requests.post")
    def test_send_returns_false_on_non_http_error(self, mock_post, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_post.side_effect = ConnectionError("Network unreachable")
        worker = mod.IntegratedTaskWorker()
        result = worker.send_to_webhook("Test message")
        assert result is False


# ===========================================================================
# parse_prompt_with_llm
# ===========================================================================

class TestParsePromptWithLlm:

    @patch("integrated_task_worker.subprocess.Popen")
    def test_parse_success(self, mock_popen, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        parsed_json = {
            "task_description": "Create API",
            "success_criteria": "Tests pass"
        }
        response_json = json.dumps({"result": json.dumps(parsed_json)})

        proc = MagicMock()
        proc.communicate.return_value = (response_json, "")
        proc.returncode = 0
        mock_popen.return_value = proc

        worker = mod.IntegratedTaskWorker()
        result = worker.parse_prompt_with_llm("Build an API for auth")
        assert result["task_description"] == "Create API"
        assert result["success_criteria"] == "Tests pass"

    @patch("integrated_task_worker.subprocess.Popen")
    def test_parse_timeout_returns_fallback(self, mock_popen, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        import subprocess
        proc = MagicMock()
        proc.communicate.side_effect = subprocess.TimeoutExpired("claude", 30)
        mock_popen.return_value = proc

        worker = mod.IntegratedTaskWorker()
        result = worker.parse_prompt_with_llm("Raw prompt text")
        assert result["task_description"] == "Raw prompt text"
        assert result["success_criteria"] == "Review and confirm task is complete"

    @patch("integrated_task_worker.subprocess.Popen")
    def test_parse_error_returns_fallback(self, mock_popen, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        proc = MagicMock()
        proc.communicate.return_value = ("not json", "")
        proc.returncode = 0
        mock_popen.return_value = proc

        worker = mod.IntegratedTaskWorker()
        result = worker.parse_prompt_with_llm("Some prompt")
        assert result["task_description"] == "Some prompt"


# ===========================================================================
# commit_task_results
# ===========================================================================

class TestCommitTaskResults:

    @patch("integrated_task_worker.subprocess.run")
    def test_commit_success(self, mock_run, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_run.side_effect = [
            MagicMock(returncode=0),  # git add
            MagicMock(returncode=0, stdout="", stderr=""),  # git commit
            MagicMock(returncode=0, stdout="abc1234\n", stderr=""),  # git rev-parse
        ]
        worker = mod.IntegratedTaskWorker()
        sha = worker.commit_task_results("task-123", tmp_path)
        assert sha == "abc1234"

    @patch("integrated_task_worker.subprocess.run")
    def test_commit_nothing_to_commit(self, mock_run, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_run.side_effect = [
            MagicMock(returncode=0),  # git add
            MagicMock(returncode=1, stdout="nothing to commit", stderr=""),  # git commit
        ]
        worker = mod.IntegratedTaskWorker()
        sha = worker.commit_task_results("task-123", tmp_path)
        assert sha is None

    @patch("integrated_task_worker.subprocess.run")
    def test_commit_exception_returns_none(self, mock_run, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_run.side_effect = Exception("Git error")
        worker = mod.IntegratedTaskWorker()
        sha = worker.commit_task_results("task-123", tmp_path)
        assert sha is None


# ===========================================================================
# poll_pending_tasks
# ===========================================================================

class TestPollPendingTasks:

    @patch("integrated_task_worker.requests.get")
    def test_poll_returns_tasks(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": [{"cr_name": "Task1"}]}
        )
        worker = mod.IntegratedTaskWorker()
        worker.current_user_id = "user-123"
        tasks = worker.poll_pending_tasks()
        assert len(tasks) == 1
        assert tasks[0]["cr_name"] == "Task1"

    @patch("integrated_task_worker.requests.get")
    def test_poll_filter_uses_webhook_user(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": []}
        )
        worker = mod.IntegratedTaskWorker()
        worker.current_user_id = "user-123"
        worker.poll_pending_tasks()
        # Verify the filter uses WEBHOOK_USER env var (testuser@example.com), not a hardcoded email
        call_kwargs = mock_get.call_args
        filter_param = call_kwargs[1]["params"]["$filter"]
        assert "testuser@example.com" in filter_param
        assert "sagik@microsoft.com" not in filter_param

    @patch("integrated_task_worker.requests.get")
    def test_poll_filter_includes_devbox_filter(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": []}
        )
        worker = mod.IntegratedTaskWorker()
        worker.current_user_id = "user-123"
        worker.poll_pending_tasks()
        # Verify filter includes devbox filter (this machine or unassigned)
        call_kwargs = mock_get.call_args
        filter_param = call_kwargs[1]["params"]["$filter"]
        assert "crb3b_devbox eq" in filter_param
        assert "crb3b_devbox eq null" in filter_param

    @patch("integrated_task_worker.requests.get")
    def test_poll_returns_empty_on_error(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.side_effect = Exception("Network error")
        worker = mod.IntegratedTaskWorker()
        worker.current_user_id = "user-123"
        tasks = worker.poll_pending_tasks()
        assert tasks == []

    @patch("integrated_task_worker.requests.get")
    def test_poll_calls_get_current_user_if_none(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        # First call for WhoAmI, second for task poll
        mock_get.side_effect = [
            MagicMock(
                raise_for_status=MagicMock(),
                json=lambda: {"UserId": "user-abc"}
            ),
            MagicMock(
                raise_for_status=MagicMock(),
                json=lambda: {"value": []}
            ),
        ]
        worker = mod.IntegratedTaskWorker()
        worker.current_user_id = None
        tasks = worker.poll_pending_tasks()
        assert worker.current_user_id == "user-abc"


# ===========================================================================
# _get_headers
# ===========================================================================

class TestGetHeaders:

    def test_returns_headers_with_token(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        headers = worker._get_headers()
        assert headers["Authorization"] == "Bearer fake-token"
        assert headers["OData-Version"] == "4.0"
        assert "Content-Type" not in headers

    def test_includes_content_type_when_specified(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        headers = worker._get_headers(content_type="application/json")
        assert headers["Content-Type"] == "application/json"

    def test_includes_if_match_when_etag_specified(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        headers = worker._get_headers(etag='W/"12345"')
        assert headers["If-Match"] == 'W/"12345"'

    def test_returns_none_when_no_token(self, monkeypatch, tmp_path):
        mod, mock_cred = _import_worker(monkeypatch, tmp_path)
        mock_cred.get_token.side_effect = Exception("Auth failed")
        worker = mod.IntegratedTaskWorker()
        worker._token_cache = None
        worker._token_expires = None
        assert worker._get_headers() is None


# ===========================================================================
# _cleanup_in_progress_task
# ===========================================================================

class TestCleanupInProgressTask:

    @patch("integrated_task_worker.requests.patch")
    def test_marks_running_task_as_failed(self, mock_patch, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_patch.return_value = MagicMock(raise_for_status=MagicMock())
        worker = mod.IntegratedTaskWorker()
        worker.current_task_id = "task-running-123"
        worker._cleanup_in_progress_task("Worker interrupted")
        # Should have called update_task with FAILED status
        call_kwargs = mock_patch.call_args
        sent_data = call_kwargs[1]["json"]
        assert sent_data["cr_status"] == mod.STATUS_FAILED
        assert "Worker interrupted" in sent_data["cr_statusmessage"]
        # Should clear task ID after cleanup
        assert worker.current_task_id is None

    def test_does_nothing_when_no_task(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        worker.current_task_id = None
        # Should not raise or call anything
        worker._cleanup_in_progress_task("No task running")
        assert worker.current_task_id is None


# ===========================================================================
# check_for_updates uses UPDATE_BRANCH
# ===========================================================================

class TestUpdateBranch:

    @patch("integrated_task_worker.subprocess.run")
    def test_uses_update_branch_env_var(self, mock_run, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        worker.current_version = "1.0.0"

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # git fetch
            MagicMock(returncode=0, stdout="1.1.0\n", stderr=""),  # git show
        ]

        worker.check_for_updates()

        # Second call should use the UPDATE_BRANCH value
        git_show_call = mock_run.call_args_list[1]
        git_show_cmd = git_show_call[0][0]
        # The branch ref should be in the command (UPDATE_BRANCH defaults to origin/users/sagik/shraga-worker)
        assert any("VERSION" in arg for arg in git_show_cmd)


# ===========================================================================
# Timeout exception handling
# ===========================================================================

class TestTimeoutHandling:

    @patch("integrated_task_worker.requests.get")
    def test_get_current_user_timeout(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        import requests as req_lib
        mock_get.side_effect = req_lib.exceptions.Timeout("Connection timed out")
        worker = mod.IntegratedTaskWorker()
        result = worker.get_current_user()
        assert result is None

    @patch("integrated_task_worker.requests.get")
    def test_poll_pending_tasks_timeout(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        import requests as req_lib
        mock_get.side_effect = req_lib.exceptions.Timeout("Connection timed out")
        worker = mod.IntegratedTaskWorker()
        worker.current_user_id = "user-123"
        tasks = worker.poll_pending_tasks()
        assert tasks == []

    @patch("integrated_task_worker.requests.patch")
    def test_update_task_timeout(self, mock_patch, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        import requests as req_lib
        mock_patch.side_effect = req_lib.exceptions.Timeout("Connection timed out")
        worker = mod.IntegratedTaskWorker()
        result = worker.update_task("task-123", status=5)
        assert result is False

    @patch("integrated_task_worker.subprocess.run")
    def test_check_for_updates_subprocess_timeout(self, mock_run, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        import subprocess as sp
        mock_run.side_effect = sp.TimeoutExpired("git", 30)
        worker = mod.IntegratedTaskWorker()
        result = worker.check_for_updates()
        assert result is False

    @patch("integrated_task_worker.subprocess.run")
    def test_apply_update_subprocess_timeout(self, mock_run, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        import subprocess as sp
        mock_run.side_effect = sp.TimeoutExpired("git", 60)
        worker = mod.IntegratedTaskWorker()
        result = worker.apply_update()
        assert result is False


# ===========================================================================
# update_task with session_summary
# ===========================================================================

class TestUpdateTaskSessionSummary:

    @patch("integrated_task_worker.requests.patch")
    def test_update_task_includes_session_summary(self, mock_patch, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_patch.return_value = MagicMock(raise_for_status=MagicMock())
        worker = mod.IntegratedTaskWorker()
        result = worker.update_task("task-123", status=7, session_summary='{"test": true}')
        assert result is True
        sent_data = mock_patch.call_args[1]["json"]
        assert sent_data["crb3b_sessionsummary"] == '{"test": true}'
        assert sent_data["cr_status"] == 7

    @patch("integrated_task_worker.requests.patch")
    def test_update_task_omits_session_summary_when_none(self, mock_patch, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_patch.return_value = MagicMock(raise_for_status=MagicMock())
        worker = mod.IntegratedTaskWorker()
        worker.update_task("task-123", status=7, session_summary=None)
        sent_data = mock_patch.call_args[1]["json"]
        assert "crb3b_sessionsummary" not in sent_data

    @patch("integrated_task_worker.requests.patch")
    def test_update_task_retries_without_summary_on_column_error(self, mock_patch, monkeypatch, tmp_path):
        """If crb3b_sessionsummary column doesn't exist, retry without it."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        import requests as req_lib

        # First call fails with "property crb3b_sessionsummary doesn't exist"
        first_call_error = Exception("The property 'crb3b_sessionsummary' does not exist")
        # Second call succeeds
        mock_patch.side_effect = [first_call_error, MagicMock(raise_for_status=MagicMock())]

        worker = mod.IntegratedTaskWorker()
        result = worker.update_task("task-123", status=7, session_summary='{"test": true}')
        assert result is True
        assert mock_patch.call_count == 2
        # Second call should not have crb3b_sessionsummary
        retry_data = mock_patch.call_args_list[1][1]["json"]
        assert "crb3b_sessionsummary" not in retry_data
        assert retry_data["cr_status"] == 7


# ===========================================================================
# build_session_summary
# ===========================================================================

class TestBuildSessionSummary:

    @patch("integrated_task_worker.requests.get")
    def test_build_summary_basic_structure(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        # Mock fetch_task_activities
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": [
                {"cr_name": "Started task"},
                {"cr_name": "Read files"},
            ]}
        )

        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "test_session"
        session_folder.mkdir()

        accumulated_stats = {
            "total_cost_usd": 0.15,
            "total_duration_ms": 45000,
            "total_api_duration_ms": 38000,
            "total_turns": 8,
            "tokens": {"input": 15000, "output": 5000, "cache_read": 3000, "cache_creation": 2000},
            "model_usage": {"claude-sonnet-4-20250514": {"cost_usd": 0.15, "input_tokens": 15000, "output_tokens": 5000}},
        }

        phases = [
            {"phase": "worker_1", "cost_usd": 0.10, "duration_ms": 30000, "turns": 5},
            {"phase": "verifier_1", "cost_usd": 0.03, "duration_ms": 10000, "turns": 2},
            {"phase": "summarizer", "cost_usd": 0.02, "duration_ms": 5000, "turns": 1},
        ]

        summary = worker.build_session_summary(
            task_id="task-001",
            terminal_status="completed",
            session_folder=session_folder,
            accumulated_stats=accumulated_stats,
            phases=phases,
            result_text="Task completed successfully with all tests passing." * 10,
            session_id="sess-abc",
        )

        assert summary["session_id"] == "sess-abc"
        assert summary["task_id"] == "task-001"
        assert summary["terminal_status"] == "completed"
        assert summary["total_cost_usd"] == 0.15
        assert summary["total_duration_ms"] == 45000
        assert summary["total_turns"] == 8
        assert summary["tokens"]["input"] == 15000
        assert len(summary["phases"]) == 3
        assert summary["phases"][0]["phase"] == "worker_1"
        assert summary["dev_box"] != ""
        assert summary["working_dir"] == str(session_folder)
        assert len(summary["result_preview"]) <= 200
        assert "timestamp" in summary
        # Activities fetched from Dataverse
        assert "Started task" in summary["activities"]
        assert "Read files" in summary["activities"]

    @patch("integrated_task_worker.requests.get")
    def test_build_summary_handles_empty_stats(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": []}
        )

        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "empty_session"
        session_folder.mkdir()

        summary = worker.build_session_summary(
            task_id="task-002",
            terminal_status="failed",
            session_folder=session_folder,
            accumulated_stats={},
            phases=[],
            result_text="Error occurred",
        )

        assert summary["terminal_status"] == "failed"
        assert summary["total_cost_usd"] == 0
        assert summary["total_turns"] == 0
        assert summary["phases"] == []
        assert summary["activities"] == []
        assert summary["num_sub_agents"] == 0

    @patch("integrated_task_worker.requests.get")
    def test_build_summary_sub_agents_count(self, mock_get, monkeypatch, tmp_path):
        """num_sub_agents = len(model_usage) - 1 (main model excluded)"""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": []}
        )

        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "multi_model_session"
        session_folder.mkdir()

        accumulated_stats = {
            "model_usage": {
                "model-main": {"cost_usd": 0.10, "input_tokens": 1000, "output_tokens": 500},
                "model-sub1": {"cost_usd": 0.03, "input_tokens": 300, "output_tokens": 100},
                "model-sub2": {"cost_usd": 0.02, "input_tokens": 200, "output_tokens": 50},
            },
        }

        summary = worker.build_session_summary(
            task_id="task-003",
            terminal_status="completed",
            session_folder=session_folder,
            accumulated_stats=accumulated_stats,
            phases=[],
            result_text="Done",
        )

        assert summary["num_sub_agents"] == 2


# ===========================================================================
# write_session_summary
# ===========================================================================

class TestWriteSessionSummary:

    @patch("integrated_task_worker.requests.get")
    @patch("integrated_task_worker.requests.patch")
    def test_writes_json_file_to_session_folder(self, mock_patch, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_patch.return_value = MagicMock(raise_for_status=MagicMock())
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": []}
        )

        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "summary_test_session"
        session_folder.mkdir()

        summary = worker.write_session_summary(
            task_id="task-write-001",
            terminal_status="completed",
            session_folder=session_folder,
            accumulated_stats={"total_cost_usd": 0.05, "total_duration_ms": 1000,
                               "total_api_duration_ms": 800, "total_turns": 2,
                               "tokens": {"input": 100, "output": 50, "cache_read": 0, "cache_creation": 0},
                               "model_usage": {}},
            phases=[{"phase": "worker_1", "cost_usd": 0.05, "duration_ms": 1000, "turns": 2}],
            result_text="All good",
            session_id="sess-xyz",
        )

        # Verify file was written
        summary_file = session_folder / "session_summary.json"
        assert summary_file.exists()

        # Verify JSON content
        content = json.loads(summary_file.read_text(encoding="utf-8"))
        assert content["task_id"] == "task-write-001"
        assert content["terminal_status"] == "completed"
        assert content["session_id"] == "sess-xyz"
        assert content["total_cost_usd"] == 0.05

        # Verify DV update was attempted
        assert mock_patch.called
        patch_data = mock_patch.call_args[1]["json"]
        assert "crb3b_sessionsummary" in patch_data

    @patch("integrated_task_worker.requests.get")
    @patch("integrated_task_worker.requests.patch")
    def test_write_summary_returns_dict(self, mock_patch, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_patch.return_value = MagicMock(raise_for_status=MagicMock())
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": []}
        )

        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "return_test"
        session_folder.mkdir()

        result = worker.write_session_summary(
            task_id="task-ret-001",
            terminal_status="failed",
            session_folder=session_folder,
            accumulated_stats={},
            phases=[],
            result_text="Error",
        )

        assert isinstance(result, dict)
        assert result["terminal_status"] == "failed"

    @patch("integrated_task_worker.requests.get")
    @patch("integrated_task_worker.requests.patch")
    def test_write_summary_graceful_on_file_write_failure(self, mock_patch, mock_get, monkeypatch, tmp_path):
        """If session folder doesn't exist, file write fails gracefully."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_patch.return_value = MagicMock(raise_for_status=MagicMock())
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": []}
        )

        worker = mod.IntegratedTaskWorker()
        # Use a non-existent folder path
        bad_folder = tmp_path / "nonexistent" / "deep" / "path"

        # Should not raise
        result = worker.write_session_summary(
            task_id="task-bad-folder",
            terminal_status="failed",
            session_folder=bad_folder,
            accumulated_stats={},
            phases=[],
            result_text="Error",
        )
        assert isinstance(result, dict)


# ===========================================================================
# write_session_log
# ===========================================================================

class TestWriteSessionLog:

    def _make_summary(self, **overrides):
        """Helper: return a minimal session summary dict with optional overrides."""
        base = {
            "session_id": "sess-log-001",
            "task_id": "task-log-001",
            "dev_box": "DEVBOX-01",
            "working_dir": "C:\\sessions\\test",
            "total_duration_ms": 90000,
            "total_cost_usd": 0.25,
            "total_api_duration_ms": 70000,
            "total_turns": 12,
            "tokens": {"input": 20000, "output": 8000, "cache_read": 5000, "cache_creation": 3000},
            "model_usage": {
                "claude-sonnet-4-20250514": {"cost_usd": 0.20, "input_tokens": 18000, "output_tokens": 7000},
                "claude-haiku-3": {"cost_usd": 0.05, "input_tokens": 2000, "output_tokens": 1000},
            },
            "num_sub_agents": 1,
            "phases": [
                {"phase": "worker_1", "cost_usd": 0.15, "duration_ms": 50000, "turns": 7},
                {"phase": "verifier_1", "cost_usd": 0.05, "duration_ms": 25000, "turns": 3},
                {"phase": "summarizer", "cost_usd": 0.05, "duration_ms": 15000, "turns": 2},
            ],
            "activities": ["Started task", "Read files", "Wrote code", "Tests passed"],
            "terminal_status": "completed",
            "result_preview": "All tests passing",
            "timestamp": "2026-02-16T10:30:00+00:00",
        }
        base.update(overrides)
        return base

    def test_writes_session_log_file(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "log_session"
        session_folder.mkdir()

        summary = self._make_summary()
        worker.write_session_log(summary, session_folder, result_text="All tests passing.")

        log_file = session_folder / "SESSION_LOG.md"
        assert log_file.exists()
        content = log_file.read_text(encoding="utf-8")
        assert "# SESSION LOG" in content

    def test_contains_task_metadata(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "meta_session"
        session_folder.mkdir()

        summary = self._make_summary()
        worker.write_session_log(summary, session_folder)

        content = (session_folder / "SESSION_LOG.md").read_text(encoding="utf-8")
        assert "task-log-001" in content
        assert "DEVBOX-01" in content
        assert "sess-log-001" in content
        assert "completed" in content

    def test_contains_session_stats(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "stats_session"
        session_folder.mkdir()

        summary = self._make_summary()
        worker.write_session_log(summary, session_folder)

        content = (session_folder / "SESSION_LOG.md").read_text(encoding="utf-8")
        assert "$0.25" in content
        assert "20,000" in content
        assert "8,000" in content
        assert "12" in content  # turns

    def test_contains_phases(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "phase_session"
        session_folder.mkdir()

        summary = self._make_summary()
        worker.write_session_log(summary, session_folder)

        content = (session_folder / "SESSION_LOG.md").read_text(encoding="utf-8")
        assert "worker_1" in content
        assert "verifier_1" in content
        assert "summarizer" in content

    def test_contains_activities(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "activity_session"
        session_folder.mkdir()

        summary = self._make_summary()
        worker.write_session_log(summary, session_folder)

        content = (session_folder / "SESSION_LOG.md").read_text(encoding="utf-8")
        assert "Started task" in content
        assert "Tests passed" in content

    def test_contains_result_text(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "result_session"
        session_folder.mkdir()

        summary = self._make_summary()
        worker.write_session_log(summary, session_folder, result_text="Final output with details.")

        content = (session_folder / "SESSION_LOG.md").read_text(encoding="utf-8")
        assert "Final output with details." in content

    def test_contains_onedrive_url(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "url_session"
        session_folder.mkdir()

        summary = self._make_summary()
        worker.write_session_log(
            summary, session_folder,
            folder_url="https://example.sharepoint.com/sessions/test"
        )

        content = (session_folder / "SESSION_LOG.md").read_text(encoding="utf-8")
        assert "https://example.sharepoint.com/sessions/test" in content
        assert "Open in OneDrive" in content

    def test_omits_onedrive_row_when_no_url(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "nourl_session"
        session_folder.mkdir()

        summary = self._make_summary()
        worker.write_session_log(summary, session_folder, folder_url="")

        content = (session_folder / "SESSION_LOG.md").read_text(encoding="utf-8")
        assert "Open in OneDrive" not in content

    def test_contains_transcript_reference(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "transcript_session"
        session_folder.mkdir()

        summary = self._make_summary()
        worker.write_session_log(summary, session_folder)

        content = (session_folder / "SESSION_LOG.md").read_text(encoding="utf-8")
        assert "cr_transcript" in content
        assert "task-log-001" in content

    def test_contains_worker_version(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "version_session"
        session_folder.mkdir()

        summary = self._make_summary()
        worker.write_session_log(summary, session_folder)

        content = (session_folder / "SESSION_LOG.md").read_text(encoding="utf-8")
        assert "Worker Version" in content

    def test_contains_model_usage(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "model_session"
        session_folder.mkdir()

        summary = self._make_summary()
        worker.write_session_log(summary, session_folder)

        content = (session_folder / "SESSION_LOG.md").read_text(encoding="utf-8")
        assert "claude-sonnet-4-20250514" in content
        assert "claude-haiku-3" in content

    def test_graceful_on_write_failure(self, monkeypatch, tmp_path):
        """Should not raise if session folder does not exist."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        bad_folder = tmp_path / "nonexistent" / "deep" / "path"

        summary = self._make_summary()
        # Should not raise
        worker.write_session_log(summary, bad_folder)

    def test_empty_summary_fields(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "empty_session"
        session_folder.mkdir()

        summary = self._make_summary(
            session_id="",
            activities=[],
            phases=[],
            model_usage={},
            result_preview="",
        )
        worker.write_session_log(summary, session_folder, result_text="")

        log_file = session_folder / "SESSION_LOG.md"
        assert log_file.exists()
        content = log_file.read_text(encoding="utf-8")
        assert "# SESSION LOG" in content
        # No activities section header when list is empty
        assert "Activity Log" not in content

    def test_falls_back_to_result_preview_when_no_result_text(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        session_folder = tmp_path / "preview_session"
        session_folder.mkdir()

        summary = self._make_summary(result_preview="Preview of result")
        worker.write_session_log(summary, session_folder, result_text="")

        content = (session_folder / "SESSION_LOG.md").read_text(encoding="utf-8")
        assert "Preview of result" in content


# ===========================================================================
# fetch_task_activities
# ===========================================================================

class TestFetchTaskActivities:

    @patch("integrated_task_worker.requests.get")
    def test_fetch_activities_success(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": [
                {"cr_name": "Started task", "createdon": "2026-01-01T00:00:00Z"},
                {"cr_name": "Read files", "createdon": "2026-01-01T00:01:00Z"},
                {"cr_name": "Wrote code", "createdon": "2026-01-01T00:02:00Z"},
            ]}
        )
        worker = mod.IntegratedTaskWorker()
        activities = worker.fetch_task_activities("task-001")
        assert len(activities) == 3
        assert activities[0] == "Started task"
        assert activities[2] == "Wrote code"

    @patch("integrated_task_worker.requests.get")
    def test_fetch_activities_truncates_long_names(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        long_name = "A" * 200
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": [{"cr_name": long_name}]}
        )
        worker = mod.IntegratedTaskWorker()
        activities = worker.fetch_task_activities("task-001")
        assert len(activities[0]) == 120

    @patch("integrated_task_worker.requests.get")
    def test_fetch_activities_returns_empty_on_error(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.side_effect = Exception("Network error")
        worker = mod.IntegratedTaskWorker()
        activities = worker.fetch_task_activities("task-001")
        assert activities == []

    @patch("integrated_task_worker.requests.get")
    def test_fetch_activities_skips_empty_names(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": [
                {"cr_name": "Valid"},
                {"cr_name": ""},
                {"cr_name": None},
            ]}
        )
        worker = mod.IntegratedTaskWorker()
        activities = worker.fetch_task_activities("task-001")
        assert len(activities) == 1
        assert activities[0] == "Valid"


# ===========================================================================
# STATUS_QUEUED constant
# ===========================================================================

class TestQueuedStatus:

    def test_queued_status_constant_exists(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        assert mod.STATUS_QUEUED == 3

    def test_queued_is_between_pending_and_running(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        assert mod.STATUS_PENDING < mod.STATUS_QUEUED < mod.STATUS_RUNNING

    def test_machine_name_constant_exists(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        assert mod.MACHINE_NAME is not None
        assert isinstance(mod.MACHINE_NAME, str)


# ===========================================================================
# claim_task (ETag-based atomic claiming)
# ===========================================================================

class TestClaimTask:

    @patch("integrated_task_worker.requests.patch")
    def test_claim_task_success(self, mock_patch, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_patch.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock()
        )
        worker = mod.IntegratedTaskWorker()
        task = {
            "cr_shraga_taskid": "task-claim-001",
            "@odata.etag": 'W/"67890"',
        }
        result = worker.claim_task(task)
        assert result is True
        # Verify If-Match header was sent
        call_headers = mock_patch.call_args[1]["headers"]
        assert call_headers["If-Match"] == 'W/"67890"'
        # Verify body sets status to Running
        call_body = mock_patch.call_args[1]["json"]
        assert call_body["cr_status"] == mod.STATUS_RUNNING

    @patch("integrated_task_worker.requests.patch")
    def test_claim_task_conflict_412(self, mock_patch, monkeypatch, tmp_path):
        """HTTP 412 means another worker claimed it first."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_patch.return_value = MagicMock(status_code=412)
        worker = mod.IntegratedTaskWorker()
        task = {
            "cr_shraga_taskid": "task-claim-002",
            "@odata.etag": 'W/"99999"',
        }
        result = worker.claim_task(task)
        assert result is False

    def test_claim_task_missing_etag(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        task = {"cr_shraga_taskid": "task-no-etag"}
        result = worker.claim_task(task)
        assert result is False

    def test_claim_task_missing_id(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        task = {"@odata.etag": 'W/"12345"'}
        result = worker.claim_task(task)
        assert result is False

    @patch("integrated_task_worker.requests.patch")
    def test_claim_task_timeout(self, mock_patch, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        import requests as req_lib
        mock_patch.side_effect = req_lib.exceptions.Timeout("timed out")
        worker = mod.IntegratedTaskWorker()
        task = {
            "cr_shraga_taskid": "task-timeout",
            "@odata.etag": 'W/"11111"',
        }
        result = worker.claim_task(task)
        assert result is False

    @patch("integrated_task_worker.requests.patch")
    def test_claim_task_network_error(self, mock_patch, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_patch.side_effect = Exception("Network error")
        worker = mod.IntegratedTaskWorker()
        task = {
            "cr_shraga_taskid": "task-net-err",
            "@odata.etag": 'W/"22222"',
        }
        result = worker.claim_task(task)
        assert result is False


# ===========================================================================
# is_devbox_busy
# ===========================================================================

class TestIsDevboxBusy:

    @patch("integrated_task_worker.requests.get")
    def test_devbox_busy_when_running_task_exists(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": [{"cr_shraga_taskid": "running-task-001"}]}
        )
        worker = mod.IntegratedTaskWorker()
        assert worker.is_devbox_busy() is True

    @patch("integrated_task_worker.requests.get")
    def test_devbox_not_busy_when_no_running_tasks(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": []}
        )
        worker = mod.IntegratedTaskWorker()
        assert worker.is_devbox_busy() is False

    @patch("integrated_task_worker.requests.get")
    def test_devbox_busy_filters_by_machine_and_running(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": []}
        )
        worker = mod.IntegratedTaskWorker()
        worker.is_devbox_busy()
        call_kwargs = mock_get.call_args
        filter_param = call_kwargs[1]["params"]["$filter"]
        assert f"cr_status eq {mod.STATUS_RUNNING}" in filter_param
        assert "crb3b_devbox eq" in filter_param

    @patch("integrated_task_worker.requests.get")
    def test_devbox_busy_returns_false_on_timeout(self, mock_get, monkeypatch, tmp_path):
        """Fail open: if we can't check, allow pickup."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        import requests as req_lib
        mock_get.side_effect = req_lib.exceptions.Timeout("timed out")
        worker = mod.IntegratedTaskWorker()
        assert worker.is_devbox_busy() is False

    @patch("integrated_task_worker.requests.get")
    def test_devbox_busy_returns_false_on_error(self, mock_get, monkeypatch, tmp_path):
        """Fail open: if we can't check, allow pickup."""
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.side_effect = Exception("Network error")
        worker = mod.IntegratedTaskWorker()
        assert worker.is_devbox_busy() is False


# ===========================================================================
# queue_task
# ===========================================================================

class TestQueueTask:

    @patch("integrated_task_worker.requests.patch")
    def test_queue_task_success(self, mock_patch, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_patch.return_value = MagicMock(raise_for_status=MagicMock())
        worker = mod.IntegratedTaskWorker()
        task = {"cr_shraga_taskid": "task-queue-001"}
        result = worker.queue_task(task)
        assert result is True
        call_body = mock_patch.call_args[1]["json"]
        assert call_body["cr_status"] == mod.STATUS_QUEUED

    @patch("integrated_task_worker.requests.patch")
    def test_queue_task_failure(self, mock_patch, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_patch.side_effect = Exception("Network error")
        worker = mod.IntegratedTaskWorker()
        task = {"cr_shraga_taskid": "task-queue-002"}
        result = worker.queue_task(task)
        assert result is False

    def test_queue_task_missing_id(self, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        worker = mod.IntegratedTaskWorker()
        result = worker.queue_task({})
        assert result is False

    @patch("integrated_task_worker.requests.patch")
    def test_queue_task_timeout(self, mock_patch, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        import requests as req_lib
        mock_patch.side_effect = req_lib.exceptions.Timeout("timed out")
        worker = mod.IntegratedTaskWorker()
        task = {"cr_shraga_taskid": "task-queue-003"}
        result = worker.queue_task(task)
        assert result is False


# ===========================================================================
# promote_queued_tasks
# ===========================================================================

class TestPromoteQueuedTasks:

    @patch("integrated_task_worker.requests.patch")
    @patch("integrated_task_worker.requests.get")
    def test_promote_queued_task_to_pending(self, mock_get, mock_patch, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": [{"cr_shraga_taskid": "queued-task-001"}]}
        )
        mock_patch.return_value = MagicMock(raise_for_status=MagicMock())
        worker = mod.IntegratedTaskWorker()
        worker.promote_queued_tasks()
        # Verify update_task was called to set status to Pending
        call_body = mock_patch.call_args[1]["json"]
        assert call_body["cr_status"] == mod.STATUS_PENDING

    @patch("integrated_task_worker.requests.get")
    def test_promote_no_queued_tasks(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": []}
        )
        worker = mod.IntegratedTaskWorker()
        # Should not raise or error
        worker.promote_queued_tasks()

    @patch("integrated_task_worker.requests.get")
    def test_promote_queries_queued_status(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=lambda: {"value": []}
        )
        worker = mod.IntegratedTaskWorker()
        worker.promote_queued_tasks()
        call_kwargs = mock_get.call_args
        filter_param = call_kwargs[1]["params"]["$filter"]
        assert f"cr_status eq {mod.STATUS_QUEUED}" in filter_param
        assert "crb3b_devbox eq" in filter_param

    @patch("integrated_task_worker.requests.get")
    def test_promote_handles_timeout(self, mock_get, monkeypatch, tmp_path):
        mod, _ = _import_worker(monkeypatch, tmp_path)
        import requests as req_lib
        mock_get.side_effect = req_lib.exceptions.Timeout("timed out")
        worker = mod.IntegratedTaskWorker()
        # Should not raise
        worker.promote_queued_tasks()
