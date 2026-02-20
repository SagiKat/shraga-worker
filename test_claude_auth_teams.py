"""Tests for claude_auth_teams.py -- Claude authentication via Teams

Tests cover:
- ClaudeAuthManager (legacy local auth)
- RemoteDevBoxAuth (RDP-based auth on the target dev box)
- TeamsClaudeAuth (RDP-first orchestration)
- DEVBOX_SETUP_SCRIPT content
"""
import pytest
from unittest.mock import patch, MagicMock, PropertyMock

from claude_auth_teams import (
    ClaudeAuthManager,
    RemoteDevBoxAuth,
    TeamsClaudeAuth,
    DEVBOX_SETUP_SCRIPT,
    get_setup_script,
)


# ===========================================================================
# ClaudeAuthManager (legacy local auth)
# ===========================================================================

class TestClaudeAuthManager:

    @patch("claude_auth_teams.subprocess.Popen")
    def test_start_auth_captures_url(self, mock_popen):
        """start_auth should extract auth URL from Claude output"""
        proc = MagicMock()
        lines = [
            "Starting authentication...\n",
            "Open this URL: https://console.anthropic.com/auth/xyz123\n",
        ]
        proc.stdout.readline = MagicMock(side_effect=lines)
        proc.poll.return_value = None
        mock_popen.return_value = proc

        mgr = ClaudeAuthManager()
        url = mgr.start_auth()
        assert "https://console.anthropic.com/auth/xyz123" in url
        assert mgr.auth_url is not None

    @patch("claude_auth_teams.subprocess.Popen")
    def test_start_auth_timeout_raises(self, mock_popen):
        """start_auth raises TimeoutError if no URL found"""
        proc = MagicMock()
        proc.stdout.readline.return_value = ""
        proc.poll.return_value = None
        mock_popen.return_value = proc

        mgr = ClaudeAuthManager()
        with patch("claude_auth_teams.time.time", side_effect=[0, 0, 31]):
            with pytest.raises(TimeoutError):
                mgr.start_auth()

    @patch("claude_auth_teams.subprocess.Popen")
    def test_start_auth_raises_if_process_exits(self, mock_popen):
        """start_auth raises if process exits before URL"""
        proc = MagicMock()
        proc.stdout.readline.return_value = ""
        proc.poll.return_value = 1
        mock_popen.return_value = proc

        mgr = ClaudeAuthManager()
        with pytest.raises(Exception, match="exited unexpectedly"):
            mgr.start_auth()

    def test_submit_code_without_start_raises(self):
        """submit_code raises if start_auth not called"""
        mgr = ClaudeAuthManager()
        with pytest.raises(RuntimeError, match="not started"):
            mgr.submit_code("ABC-123")

    @patch("claude_auth_teams.subprocess.Popen")
    def test_submit_code_success(self, mock_popen):
        """submit_code returns True on successful auth"""
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.poll.side_effect = [None, 0]
        type(proc).returncode = PropertyMock(return_value=0)
        mock_popen.return_value = proc

        mgr = ClaudeAuthManager()
        mgr.process = proc

        with patch("claude_auth_teams.time.time", side_effect=[0, 0, 0.2]):
            result = mgr.submit_code("ABC-123")
        assert result is True

    @patch("claude_auth_teams.subprocess.Popen")
    def test_submit_code_failure(self, mock_popen):
        """submit_code returns False on auth failure"""
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.poll.side_effect = [None, 1]
        type(proc).returncode = PropertyMock(return_value=1)
        proc.stderr.read.return_value = "Auth failed"
        mock_popen.return_value = proc

        mgr = ClaudeAuthManager()
        mgr.process = proc

        with patch("claude_auth_teams.time.time", side_effect=[0, 0, 0.2]):
            result = mgr.submit_code("WRONG-CODE")
        assert result is False

    def test_cancel_terminates_process(self):
        """cancel terminates the subprocess"""
        mgr = ClaudeAuthManager()
        proc = MagicMock()
        mgr.process = proc
        mgr.cancel()
        proc.terminate.assert_called_once()
        assert mgr.process is None

    def test_cancel_noop_without_process(self):
        """cancel does nothing if no process"""
        mgr = ClaudeAuthManager()
        mgr.cancel()  # should not raise


# ===========================================================================
# RemoteDevBoxAuth
# ===========================================================================

class TestRemoteDevBoxAuth:

    def test_get_connection_url_from_init(self):
        """When connection_url is passed at init, it is returned directly."""
        auth = RemoteDevBoxAuth(connection_url="https://devbox.microsoft.com/connect?devbox=test")
        url = auth.get_connection_url("user-id", "test")
        assert url == "https://devbox.microsoft.com/connect?devbox=test"

    def test_get_connection_url_via_manager(self):
        """When no connection_url is passed, it uses DevBoxManager."""
        mock_mgr = MagicMock()
        mock_mgr.get_connection_url.return_value = "https://devbox.microsoft.com/connect?devbox=foo"

        auth = RemoteDevBoxAuth(devbox_manager=mock_mgr)
        url = auth.get_connection_url("user-aad-id", "foo")
        assert url == "https://devbox.microsoft.com/connect?devbox=foo"
        mock_mgr.get_connection_url.assert_called_once_with("user-aad-id", "foo")

    def test_get_connection_url_raises_without_manager_or_url(self):
        """When neither connection_url nor devbox_manager is provided, raise."""
        auth = RemoteDevBoxAuth()
        with pytest.raises(RuntimeError, match="Cannot resolve connection URL"):
            auth.get_connection_url("uid", "box")

    def test_build_auth_message_contains_url(self):
        """The auth message includes the RDP connection URL."""
        auth = RemoteDevBoxAuth()
        msg = auth.build_auth_message("https://devbox.microsoft.com/connect?devbox=test")
        assert "https://devbox.microsoft.com/connect?devbox=test" in msg

    def test_build_auth_message_contains_claude_login(self):
        """The auth message instructs the user to run claude /login."""
        auth = RemoteDevBoxAuth()
        msg = auth.build_auth_message("https://example.com")
        assert "claude /login" in msg

    def test_build_auth_message_contains_setup_steps(self):
        """The auth message includes setup instructions."""
        auth = RemoteDevBoxAuth()
        msg = auth.build_auth_message("https://example.com")
        assert "Shraga-Authenticate" in msg
        assert "done" in msg.lower()

    def test_build_setup_script_message(self):
        """The setup script message includes the PS1 script."""
        auth = RemoteDevBoxAuth()
        msg = auth.build_setup_script_message()
        assert "pip install" in msg
        assert "shraga-worker" in msg

    def test_connection_url_cached_after_first_call(self):
        """Once resolved, the connection URL is cached."""
        mock_mgr = MagicMock()
        mock_mgr.get_connection_url.return_value = "https://cached.example.com"

        auth = RemoteDevBoxAuth(devbox_manager=mock_mgr)
        auth.get_connection_url("uid", "box")
        auth.get_connection_url("uid", "box")
        # Manager should only be called once
        assert mock_mgr.get_connection_url.call_count == 1


# ===========================================================================
# TeamsClaudeAuth -- RDP-first auth
# ===========================================================================

class TestTeamsClaudeAuth:

    def test_init_stores_params(self):
        send_fn = MagicMock()
        auth = TeamsClaudeAuth(send_fn, "user-123")
        assert auth.user_id == "user-123"
        assert auth.send_message == send_fn

    def test_request_auth_uses_rdp_when_connection_url_available(self):
        """When connection_url is provided, RDP auth is used (not local)."""
        send_fn = MagicMock()
        auth = TeamsClaudeAuth(
            send_fn, "user-123",
            devbox_name="shraga-test",
            user_azure_ad_id="aad-id",
            connection_url="https://devbox.microsoft.com/connect?devbox=shraga-test",
        )
        result = auth.request_authentication()
        assert result is True
        assert auth.used_rdp_auth is True
        # Message should contain the RDP URL
        msg = send_fn.call_args[0][1]
        assert "devbox.microsoft.com" in msg
        assert "claude /login" in msg

    def test_request_auth_uses_rdp_via_manager(self):
        """When devbox_manager is provided, RDP auth resolves the URL."""
        send_fn = MagicMock()
        mock_mgr = MagicMock()
        mock_mgr.get_connection_url.return_value = "https://devbox.microsoft.com/connect?devbox=mgr-box"

        auth = TeamsClaudeAuth(
            send_fn, "user-123",
            devbox_name="mgr-box",
            user_azure_ad_id="aad-id",
            devbox_manager=mock_mgr,
        )
        result = auth.request_authentication()
        assert result is True
        assert auth.used_rdp_auth is True
        msg = send_fn.call_args[0][1]
        assert "devbox.microsoft.com" in msg

    @patch.object(ClaudeAuthManager, "start_auth", return_value="https://auth.example.com")
    def test_request_auth_falls_back_to_device_code_without_rdp_info(self, mock_start):
        """Without connection_url or manager, falls back to device-code."""
        send_fn = MagicMock()
        auth = TeamsClaudeAuth(send_fn, "user-123")
        result = auth.request_authentication()
        assert result is True
        # used_rdp_auth should be False since we fell back to device code
        assert auth.used_rdp_auth is False
        msg = send_fn.call_args[0][1]
        assert "https://auth.example.com" in msg

    @patch.object(ClaudeAuthManager, "start_auth", side_effect=Exception("Network error"))
    def test_request_auth_total_failure(self, mock_start):
        """Without RDP info and device-code failure, returns False."""
        send_fn = MagicMock()
        auth = TeamsClaudeAuth(send_fn, "user-123")
        result = auth.request_authentication()
        assert result is False
        msg = send_fn.call_args[0][1]
        assert "Failed" in msg

    @patch.object(ClaudeAuthManager, "submit_code", return_value=True)
    def test_handle_user_code_success(self, mock_submit):
        send_fn = MagicMock()
        auth = TeamsClaudeAuth(send_fn, "user-123")
        result = auth.handle_user_code("  ABC-123  ")
        assert result is True
        mock_submit.assert_called_once_with("ABC-123")
        msg = send_fn.call_args[0][1]
        assert "Complete" in msg

    @patch.object(ClaudeAuthManager, "submit_code", return_value=False)
    def test_handle_user_code_failure(self, mock_submit):
        send_fn = MagicMock()
        auth = TeamsClaudeAuth(send_fn, "user-123")
        result = auth.handle_user_code("BAD-CODE")
        assert result is False
        msg = send_fn.call_args[0][1]
        assert "Failed" in msg

    def test_handle_user_done(self):
        """handle_user_done returns a confirmation message."""
        send_fn = MagicMock()
        auth = TeamsClaudeAuth(send_fn, "user-123")
        msg = auth.handle_user_done()
        assert "setup" in msg.lower() or "ready" in msg.lower()

    def test_fell_back_to_rdp_is_alias_for_used_rdp_auth(self):
        """The fell_back_to_rdp property is a backward-compat alias."""
        send_fn = MagicMock()
        auth = TeamsClaudeAuth(
            send_fn, "user-123",
            connection_url="https://example.com",
        )
        auth.request_authentication()
        assert auth.fell_back_to_rdp == auth.used_rdp_auth


# ===========================================================================
# DEVBOX_SETUP_SCRIPT
# ===========================================================================

class TestDevBoxSetupScript:

    def test_script_installs_pip_packages(self):
        assert "pip install" in DEVBOX_SETUP_SCRIPT
        assert "requests" in DEVBOX_SETUP_SCRIPT
        assert "azure-identity" in DEVBOX_SETUP_SCRIPT
        assert "watchdog" in DEVBOX_SETUP_SCRIPT

    def test_script_clones_repo(self):
        assert "git clone" in DEVBOX_SETUP_SCRIPT
        assert "shraga-worker" in DEVBOX_SETUP_SCRIPT

    def test_script_creates_scheduled_task(self):
        assert "Register-ScheduledTask" in DEVBOX_SETUP_SCRIPT
        assert "ShragaWorker" in DEVBOX_SETUP_SCRIPT

    def test_get_setup_script_returns_same(self):
        assert get_setup_script() == DEVBOX_SETUP_SCRIPT
