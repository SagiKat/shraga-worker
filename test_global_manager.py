"""
Tests for Global Manager (agentic fallback handler).

All external dependencies (Azure, Dataverse, Claude CLI) are mocked.
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta

import pytest

# Add global-manager to path
sys.path.insert(0, str(Path(__file__).parent / "global-manager"))

from conftest import FakeAccessToken, FakeResponse
from orchestrator_devbox import DevBoxInfo


# -- Fixtures ----------------------------------------------------------------

SAMPLE_CONVERSATION_ID = "conv-0001-0002-0003-000000000001"
SAMPLE_MCS_CONV_ID = "mcs-conv-abc123"

SAMPLE_STALE_MSG = {
    "cr_shraga_conversationid": SAMPLE_CONVERSATION_ID,
    "cr_useremail": "newuser@example.com",
    "cr_mcs_conversation_id": SAMPLE_MCS_CONV_ID,
    "cr_message": "hello, I want to create a task",
    "cr_direction": "Inbound",
    "cr_status": "Unclaimed",
    "@odata.etag": 'W/"12345"',
    "createdon": "2026-02-15T09:59:00Z",  # > 15s ago
}


@pytest.fixture
def mock_credential():
    cred = MagicMock()
    cred.get_token.return_value = FakeAccessToken()
    return cred


@pytest.fixture
def manager(mock_credential, monkeypatch):
    """Create a GlobalManager with mocked credentials."""
    with patch("global_manager.get_credential", return_value=mock_credential):
        from global_manager import GlobalManager
        mgr = GlobalManager()
    return mgr


# -- Auth Tests ---------------------------------------------------------------

class TestAuth:
    def test_get_token_success(self, manager):
        assert manager.get_token() == "fake-token-12345"

    def test_get_token_caches(self, manager):
        manager.get_token()
        manager.get_token()
        assert manager.credential.get_token.call_count == 1

    def test_get_token_refreshes_expired(self, manager):
        manager.get_token()
        manager._token_expires = datetime.now(timezone.utc) - timedelta(minutes=1)
        manager.get_token()
        assert manager.credential.get_token.call_count == 2


# -- Polling Tests ------------------------------------------------------------

class TestPolling:
    @patch("global_manager.requests.get")
    def test_poll_stale_unclaimed_returns_old_messages(self, mock_get, manager):
        mock_get.return_value = FakeResponse(json_data={"value": [SAMPLE_STALE_MSG]})
        msgs = manager.poll_stale_unclaimed()
        assert len(msgs) == 1

    @patch("global_manager.requests.get")
    def test_poll_filters_by_age(self, mock_get, manager):
        mock_get.return_value = FakeResponse(json_data={"value": []})
        manager.poll_stale_unclaimed()
        url = mock_get.call_args[0][0]
        assert "createdon lt" in url
        assert "cr_status eq 'Unclaimed'" in url

    @patch("global_manager.requests.get")
    def test_poll_handles_timeout(self, mock_get, manager):
        import requests as req
        mock_get.side_effect = req.exceptions.Timeout()
        assert manager.poll_stale_unclaimed() == []

    @patch("global_manager.requests.get")
    def test_poll_handles_error(self, mock_get, manager):
        mock_get.side_effect = Exception("network error")
        assert manager.poll_stale_unclaimed() == []


# -- Claim Tests --------------------------------------------------------------

class TestClaim:
    @patch("global_manager.requests.patch")
    def test_claim_success(self, mock_patch, manager):
        mock_patch.return_value = FakeResponse(status_code=204)
        assert manager.claim_message(SAMPLE_STALE_MSG) is True

    @patch("global_manager.requests.patch")
    def test_claim_sets_global_id(self, mock_patch, manager):
        mock_patch.return_value = FakeResponse(status_code=204)
        manager.claim_message(SAMPLE_STALE_MSG)
        body = mock_patch.call_args[1]["json"]
        assert body["cr_claimed_by"].startswith("global:")

    @patch("global_manager.requests.patch")
    def test_claim_conflict(self, mock_patch, manager):
        mock_patch.return_value = FakeResponse(status_code=412)
        assert manager.claim_message(SAMPLE_STALE_MSG) is False

    def test_claim_no_etag(self, manager):
        msg = {**SAMPLE_STALE_MSG}
        del msg["@odata.etag"]
        assert manager.claim_message(msg) is False

    def test_claim_no_id(self, manager):
        msg = {**SAMPLE_STALE_MSG}
        del msg["cr_shraga_conversationid"]
        assert manager.claim_message(msg) is False


# -- Response Tests -----------------------------------------------------------

class TestResponse:
    @patch("global_manager.requests.post")
    def test_send_response(self, mock_post, manager):
        mock_post.return_value = FakeResponse(json_data={})
        result = manager.send_response(
            in_reply_to=SAMPLE_CONVERSATION_ID,
            mcs_conversation_id=SAMPLE_MCS_CONV_ID,
            user_email="newuser@example.com",
            text="Welcome!",
        )
        assert result is not None
        body = mock_post.call_args[1]["json"]
        assert body["cr_direction"] == "Outbound"
        assert body["cr_message"] == "Welcome!"

    @patch("global_manager.requests.post")
    def test_send_response_error(self, mock_post, manager):
        mock_post.side_effect = Exception("error")
        assert manager.send_response("id", "conv", "email", "text") is None


# -- Agentic Message Processing Tests ----------------------------------------

class TestProcessMessage:
    @patch("global_manager.requests.post")
    @patch("global_manager.requests.patch")
    def test_process_uses_claude_and_sends_response(self, mock_patch, mock_post, manager):
        """Claude is called and its response is sent to the user."""
        mock_post.return_value = FakeResponse(json_data={})
        mock_patch.return_value = FakeResponse(status_code=204)

        with patch.object(manager, "_call_claude_with_tools", return_value="Hello! I can help you."):
            manager.process_message(SAMPLE_STALE_MSG)

        # Response was sent
        body = mock_post.call_args[1]["json"]
        assert body["cr_message"] == "Hello! I can help you."

    @patch("global_manager.requests.post")
    @patch("global_manager.requests.patch")
    def test_process_fallback_when_claude_unavailable(self, mock_patch, mock_post, manager):
        """When Claude is unavailable, the single fallback message is sent."""
        mock_post.return_value = FakeResponse(json_data={})
        mock_patch.return_value = FakeResponse(status_code=204)

        with patch.object(manager, "_call_claude_with_tools", return_value=None):
            manager.process_message(SAMPLE_STALE_MSG)

        body = mock_post.call_args[1]["json"]
        assert body["cr_message"] == "The system is temporarily unavailable, please try again shortly."

    @patch("global_manager.requests.patch")
    def test_process_empty_message(self, mock_patch, manager):
        empty_msg = {**SAMPLE_STALE_MSG, "cr_message": ""}
        mock_patch.return_value = FakeResponse(status_code=204)
        manager.process_message(empty_msg)
        mock_patch.assert_called_once()

    def test_new_user_not_added_to_known_without_tool_call(self, manager):
        """Users are NOT added to _known_users unless mark_user_onboarded tool is called."""
        with patch.object(manager, "send_response"), \
             patch.object(manager, "mark_processed"), \
             patch.object(manager, "_call_claude_with_tools", return_value="I can help."):
            manager.process_message(SAMPLE_STALE_MSG)
        assert "newuser@example.com" not in manager._known_users


# -- Azure AD ID Resolution Tests --------------------------------------------

class TestResolveAzureAdId:
    @patch("global_manager.requests.get")
    def test_resolve_success(self, mock_get, manager):
        mock_get.return_value = FakeResponse(
            json_data={"id": "aad-guid-abcd-1234", "displayName": "New User"}
        )
        result = manager.resolve_azure_ad_id("newuser@example.com")
        assert result == "aad-guid-abcd-1234"

        # Verify it called Graph API with the right URL
        call_url = mock_get.call_args[0][0]
        assert "graph.microsoft.com/v1.0/users/newuser@example.com" in call_url

    @patch("global_manager.requests.get")
    def test_resolve_uses_graph_token(self, mock_get, manager):
        mock_get.return_value = FakeResponse(
            json_data={"id": "guid-123"}
        )
        manager.resolve_azure_ad_id("user@example.com")

        # credential.get_token should have been called with graph scope
        scopes = [
            call.args[0]
            for call in manager.credential.get_token.call_args_list
        ]
        assert "https://graph.microsoft.com/.default" in scopes

    @patch("global_manager.requests.get")
    def test_resolve_http_error_raises(self, mock_get, manager):
        mock_get.return_value = FakeResponse(
            status_code=404, text="Not Found"
        )
        with pytest.raises(Exception, match="Graph API error"):
            manager.resolve_azure_ad_id("nobody@example.com")

    @patch("global_manager.requests.get")
    def test_resolve_missing_id_raises(self, mock_get, manager):
        mock_get.return_value = FakeResponse(
            json_data={"displayName": "No ID User"}
        )
        with pytest.raises(Exception, match="missing 'id' field"):
            manager.resolve_azure_ad_id("noid@example.com")


# -- Tool Implementation Tests -----------------------------------------------

class TestTools:
    """Test the individual tool implementations directly."""

    def _make_manager_with_devbox(self, manager):
        """Helper to attach a mocked DevBoxManager."""
        mock_devbox = MagicMock()
        manager.devbox_manager = mock_devbox
        return mock_devbox

    @patch("global_manager.requests.get")
    def test_tool_get_user_state_found(self, mock_get, manager):
        mock_get.return_value = FakeResponse(json_data={
            "value": [{
                "crb3b_shragauserid": "row-123",
                "crb3b_onboardingstep": "provisioning",
                "crb3b_devboxname": "shraga-newuser",
                "crb3b_azureadid": "aad-guid",
                "crb3b_connectionurl": "",
                "crb3b_authurl": "",
            }]
        })
        result = manager._tool_get_user_state("newuser@example.com")
        assert result["found"] is True
        assert result["onboarding_step"] == "provisioning"

    @patch("global_manager.requests.get")
    def test_tool_get_user_state_not_found(self, mock_get, manager):
        mock_get.return_value = FakeResponse(json_data={"value": []})
        result = manager._tool_get_user_state("unknown@example.com")
        assert result["found"] is False

    @patch("global_manager.requests.get")
    @patch("global_manager.requests.post")
    @patch("global_manager.requests.patch")
    def test_tool_provision_devbox(self, mock_patch, mock_post, mock_get, manager):
        mock_devbox = self._make_manager_with_devbox(manager)
        mock_devbox.provision_devbox.return_value = {"name": "shraga-newuser"}

        # Graph API for resolve_azure_ad_id
        mock_get.return_value = FakeResponse(json_data={"id": "real-aad-guid-5678"})
        mock_post.return_value = FakeResponse(json_data={})
        mock_patch.return_value = FakeResponse(status_code=204)

        result = manager._tool_provision_devbox("newuser@example.com")
        assert result["success"] is True
        assert result["devbox_name"] == "shraga-newuser"
        mock_devbox.provision_devbox.assert_called_once()
        # Verify it used the resolved GUID
        call_args = mock_devbox.provision_devbox.call_args
        assert call_args[0][0] == "real-aad-guid-5678"

    def test_tool_provision_devbox_not_configured(self, manager):
        """Returns error when devbox_manager is None."""
        result = manager._tool_provision_devbox("newuser@example.com")
        assert "error" in result

    def test_tool_check_devbox_status(self, manager):
        mock_devbox = self._make_manager_with_devbox(manager)
        mock_devbox.get_devbox_status.return_value = DevBoxInfo(
            name="shraga-newuser",
            user_id="aad-guid",
            status="Running",
            connection_url="https://devbox.microsoft.com/connect?devbox=shraga-newuser",
            provisioning_state="Succeeded",
        )
        result = manager._tool_check_devbox_status("shraga-newuser", "aad-guid")
        assert result["provisioning_state"] == "Succeeded"
        assert "devbox.microsoft.com" in result["connection_url"]

    def test_tool_apply_customizations(self, manager):
        mock_devbox = self._make_manager_with_devbox(manager)
        mock_devbox.apply_customizations.return_value = {"status": "Running"}
        result = manager._tool_apply_customizations("shraga-newuser", "aad-guid")
        assert result["success"] is True

    def test_tool_check_customization_status(self, manager):
        mock_devbox = self._make_manager_with_devbox(manager)
        mock_devbox.get_customization_status.return_value = {"status": "Succeeded"}
        result = manager._tool_check_customization_status("shraga-newuser", "aad-guid")
        assert result["status"] == "Succeeded"

    def test_tool_get_rdp_auth_message(self, manager):
        result = manager._tool_get_rdp_auth_message(
            "https://devbox.microsoft.com/connect?devbox=shraga-newuser"
        )
        msg = result["message"]
        assert "devbox.microsoft.com" in msg
        assert "Shraga-Authenticate" in msg
        assert "done" in msg.lower()

    @patch("global_manager.requests.patch")
    @patch("global_manager.requests.get")
    def test_tool_mark_user_onboarded(self, mock_get, mock_patch, manager):
        mock_get.return_value = FakeResponse(json_data={"value": []})
        mock_patch.return_value = FakeResponse(status_code=204)
        result = manager._tool_mark_user_onboarded("newuser@example.com")
        assert result["success"] is True
        assert "newuser@example.com" in manager._known_users


# -- Agentic Onboarding Integration Tests ------------------------------------

class TestAgenticOnboarding:
    """Verify the agentic flow works end-to-end with mocked Claude."""

    def _make_manager_with_devbox(self, manager):
        mock_devbox = MagicMock()
        manager.devbox_manager = mock_devbox
        return mock_devbox

    @patch("global_manager.requests.get")
    @patch("global_manager.requests.post")
    @patch("global_manager.requests.patch")
    def test_claude_can_check_status_via_tools(self, mock_patch, mock_post, mock_get, manager):
        """Simulate Claude calling the check_devbox_status tool."""
        mock_devbox = self._make_manager_with_devbox(manager)
        mock_devbox.get_devbox_status.return_value = MagicMock(
            provisioning_state="Succeeded", status="Running",
            connection_url="https://rdp.example.com"
        )

        mock_get.return_value = FakeResponse(json_data={"value": []})
        mock_post.return_value = FakeResponse(json_data={})
        mock_patch.return_value = FakeResponse(status_code=204)

        # Simulate Claude: first call returns a tool_call, second returns final response
        call_count = [0]
        def mock_claude(text, system_prompt):
            call_count[0] += 1
            if call_count[0] == 1:
                return json.dumps({
                    "tool_calls": [{"name": "check_devbox_status", "arguments": {"devbox_name": "shraga-newuser", "azure_ad_id": "aad-guid"}}]
                })
            else:
                return json.dumps({
                    "response": "Your dev box shraga-newuser is ready and running."
                })

        with patch.object(manager, "_call_claude", side_effect=mock_claude):
            result = manager._call_claude_with_tools(
                "User email: newuser@example.com\nUser message: \"status?\"",
                "system prompt",
                {"user_email": "newuser@example.com", "row_id": "r1", "mcs_conv_id": "c1"},
            )

        assert "shraga-newuser" in result
        mock_devbox.get_devbox_status.assert_called_once()

    @patch("global_manager.requests.get")
    @patch("global_manager.requests.post")
    @patch("global_manager.requests.patch")
    def test_claude_plain_text_response(self, mock_patch, mock_post, mock_get, manager):
        """When Claude returns plain text (not JSON), it's used directly."""
        mock_post.return_value = FakeResponse(json_data={})
        mock_patch.return_value = FakeResponse(status_code=204)

        with patch.object(manager, "_call_claude", return_value="Hello, welcome!"):
            result = manager._call_claude_with_tools(
                "User context",
                "system prompt",
                {"user_email": "user@example.com", "row_id": "r1", "mcs_conv_id": "c1"},
            )

        assert result == "Hello, welcome!"

    @patch("global_manager.requests.get")
    @patch("global_manager.requests.post")
    @patch("global_manager.requests.patch")
    def test_mark_onboarded_via_tool(self, mock_patch, mock_post, mock_get, manager):
        """Claude can mark a user as onboarded via tool."""
        mock_get.return_value = FakeResponse(json_data={"value": []})
        mock_patch.return_value = FakeResponse(status_code=204)

        call_count = [0]
        def mock_claude(text, system_prompt):
            call_count[0] += 1
            if call_count[0] == 1:
                return json.dumps({
                    "tool_calls": [{"name": "mark_user_onboarded", "arguments": {"user_email": "newuser@example.com"}}]
                })
            else:
                return json.dumps({
                    "response": "Setup complete! Your personal assistant is ready."
                })

        with patch.object(manager, "_call_claude", side_effect=mock_claude):
            result = manager._call_claude_with_tools(
                "User context",
                "system prompt",
                {"user_email": "newuser@example.com", "row_id": "r1", "mcs_conv_id": "c1"},
            )

        assert "ready" in result.lower()
        assert "newuser@example.com" in manager._known_users

    @patch("global_manager.requests.get")
    @patch("global_manager.requests.post")
    @patch("global_manager.requests.patch")
    def test_completed_onboarding_updates_dataverse(self, mock_patch, mock_post, mock_get, manager):
        """When mark_user_onboarded tool is called, Dataverse is updated."""
        mock_get.return_value = FakeResponse(json_data={"value": []})
        mock_patch.return_value = FakeResponse(status_code=204)
        mock_post.return_value = FakeResponse(json_data={})

        call_count = [0]
        def mock_claude(text, system_prompt):
            call_count[0] += 1
            if call_count[0] == 1:
                return json.dumps({
                    "tool_calls": [{"name": "mark_user_onboarded", "arguments": {"user_email": "newuser@example.com"}}]
                })
            else:
                return json.dumps({"response": "Done!"})

        with patch.object(manager, "_call_claude", side_effect=mock_claude):
            manager._call_claude_with_tools(
                "context", "prompt",
                {"user_email": "newuser@example.com", "row_id": "r1", "mcs_conv_id": "c1"},
            )

        # Find the call that updated onboarding step to "completed"
        # Could be via PATCH (existing row) or POST (new row) depending on cache
        found_completed = False
        for call_obj in mock_patch.call_args_list:
            body = call_obj[1].get("json", {})
            if body.get("crb3b_onboardingstep") == "completed":
                found_completed = True
                break
        if not found_completed:
            for call_obj in mock_post.call_args_list:
                body = call_obj[1].get("json", {})
                if body.get("crb3b_onboardingstep") == "completed":
                    found_completed = True
                    break
        assert found_completed, "Expected Dataverse update with crb3b_onboardingstep='completed'"


# -- RDP Auth Message Tests ---------------------------------------------------

class TestRdpAuthMessage:
    """Verify the RDP auth message tool produces correct content."""

    def test_rdp_message_contains_connection_url(self, manager):
        result = manager._tool_get_rdp_auth_message(
            "https://devbox.microsoft.com/connect?devbox=shraga-newuser"
        )
        assert "devbox.microsoft.com/connect" in result["message"]

    def test_rdp_message_contains_claude_login(self, manager):
        result = manager._tool_get_rdp_auth_message("https://example.com")
        assert "claude /login" in result["message"]

    def test_rdp_message_contains_setup_instructions(self, manager):
        result = manager._tool_get_rdp_auth_message("https://example.com")
        msg = result["message"]
        assert "Shraga-Authenticate" in msg
        assert "done" in msg.lower()

    def test_rdp_message_fallback_url(self, manager):
        """When no connection_url is in state, a default can be constructed."""
        url = "https://devbox.microsoft.com/connect?devbox=shraga-newuser"
        result = manager._tool_get_rdp_auth_message(url)
        assert "devbox.microsoft.com/connect?devbox=shraga-newuser" in result["message"]


# -- Constructor Tests --------------------------------------------------------

class TestConstructor:
    def test_manager_id(self, manager):
        assert manager.manager_id == "global"

    def test_known_users_empty(self, manager):
        assert len(manager._known_users) == 0


# -- Device Code Auth Tests ---------------------------------------------------

class TestGetCredential:
    """Tests for the get_credential() fallback logic."""

    def test_uses_default_credential_when_available(self):
        """When DefaultAzureCredential works, it should be returned directly."""
        fake_cred = MagicMock()
        fake_cred.get_token.return_value = FakeAccessToken()

        with patch("global_manager.DefaultAzureCredential", return_value=fake_cred):
            from global_manager import get_credential
            result = get_credential()

        assert result is fake_cred
        fake_cred.get_token.assert_called_once_with("https://management.azure.com/.default")

    def test_falls_back_to_device_code_on_failure(self):
        """When DefaultAzureCredential fails, DeviceCodeCredential is used."""
        broken_cred = MagicMock()
        broken_cred.get_token.side_effect = Exception("No credentials")

        device_cred = MagicMock()
        device_cred.get_token.return_value = FakeAccessToken()

        with patch("global_manager.DefaultAzureCredential", return_value=broken_cred), \
             patch("global_manager.DeviceCodeCredential", return_value=device_cred) as mock_dc:
            from global_manager import get_credential
            result = get_credential()

        assert result is device_cred
        mock_dc.assert_called_once()
        # Verify the tenant ID is Microsoft's
        call_kwargs = mock_dc.call_args[1]
        assert call_kwargs["tenant_id"] == "72f988bf-86f1-41af-91ab-2d7cd011db47"
        # Verify prompt_callback was set
        assert "prompt_callback" in call_kwargs
        # Verify it forced initial authentication
        device_cred.get_token.assert_called_once_with("https://management.azure.com/.default")

    def test_device_code_credential_is_process_scoped(self):
        """The credential is an in-memory object, not system-wide state."""
        broken_cred = MagicMock()
        broken_cred.get_token.side_effect = Exception("No credentials")

        device_cred_1 = MagicMock()
        device_cred_1.get_token.return_value = FakeAccessToken(token="token-1")
        device_cred_2 = MagicMock()
        device_cred_2.get_token.return_value = FakeAccessToken(token="token-2")

        from global_manager import get_credential

        # Simulate two separate calls (two GM instances)
        with patch("global_manager.DefaultAzureCredential", return_value=broken_cred), \
             patch("global_manager.DeviceCodeCredential", return_value=device_cred_1):
            cred1 = get_credential()

        with patch("global_manager.DefaultAzureCredential", return_value=broken_cred), \
             patch("global_manager.DeviceCodeCredential", return_value=device_cred_2):
            cred2 = get_credential()

        # Each call gets its own credential object
        assert cred1 is not cred2
        assert cred1 is device_cred_1
        assert cred2 is device_cred_2

    def test_devbox_manager_receives_shared_credential(self, monkeypatch):
        """When DevBoxManager is created, it receives the GM's credential."""
        fake_cred = MagicMock()
        fake_cred.get_token.return_value = FakeAccessToken()

        monkeypatch.setenv("DEVCENTER_ENDPOINT", "https://dc.example.com")
        monkeypatch.setenv("DEVBOX_PROJECT", "test-project")

        with patch("global_manager.get_credential", return_value=fake_cred), \
             patch("global_manager.DevBoxManager") as mock_dbm_cls:
            from global_manager import GlobalManager
            mgr = GlobalManager()

        # Verify DevBoxManager was created with the credential kwarg
        mock_dbm_cls.assert_called_once()
        call_kwargs = mock_dbm_cls.call_args[1]
        assert call_kwargs.get("credential") is fake_cred

    def test_device_code_callback_format(self):
        """The device code callback should print the verification URI and code."""
        broken_cred = MagicMock()
        broken_cred.get_token.side_effect = Exception("No credentials")

        device_cred = MagicMock()
        device_cred.get_token.return_value = FakeAccessToken()

        captured_callback = None

        def capture_dc(**kwargs):
            nonlocal captured_callback
            captured_callback = kwargs.get("prompt_callback")
            return device_cred

        with patch("global_manager.DefaultAzureCredential", return_value=broken_cred), \
             patch("global_manager.DeviceCodeCredential", side_effect=capture_dc):
            from global_manager import get_credential
            get_credential()

        assert captured_callback is not None

        # Call the callback and verify it doesn't crash
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            captured_callback("https://microsoft.com/devicelogin", "ABCD1234", 900)

        output = buf.getvalue()
        assert "https://microsoft.com/devicelogin" in output
        assert "ABCD1234" in output
        assert "15 minutes" in output  # 900 // 60 = 15


# -- JSON Parsing Tests -------------------------------------------------------

class TestJsonParsing:
    """Test the _try_parse_json helper."""

    def test_valid_json(self, manager):
        result = manager._try_parse_json('{"response": "hello"}')
        assert result == {"response": "hello"}

    def test_invalid_json_returns_none(self, manager):
        result = manager._try_parse_json("This is plain text")
        assert result is None

    def test_json_in_code_block(self, manager):
        text = '```json\n{"response": "hello"}\n```'
        result = manager._try_parse_json(text)
        assert result == {"response": "hello"}

    def test_empty_string(self, manager):
        result = manager._try_parse_json("")
        assert result is None


# -- System Prompt Tests -------------------------------------------------------

class TestSystemPrompt:
    def test_system_prompt_with_devbox_manager(self, manager):
        manager.devbox_manager = MagicMock()
        prompt = manager._build_system_prompt("user@example.com", has_devbox_manager=True)
        assert "provision" in prompt.lower()
        assert "NOT configured" not in prompt

    def test_system_prompt_without_devbox_manager(self, manager):
        prompt = manager._build_system_prompt("user@example.com", has_devbox_manager=False)
        assert "NOT configured" in prompt
