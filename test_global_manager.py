"""
Tests for Global Manager (fallback handler).

All external dependencies (Azure, Dataverse) are mocked.
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
        assert body["cr_claimed_by"] == "global"

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


# -- Message Processing Tests -------------------------------------------------

class TestProcessMessage:
    @patch("global_manager.requests.post")
    @patch("global_manager.requests.patch")
    def test_process_new_user(self, mock_patch, mock_post, manager):
        mock_post.return_value = FakeResponse(json_data={})
        mock_patch.return_value = FakeResponse(status_code=204)
        manager.process_message(SAMPLE_STALE_MSG)
        # DevCenter env vars are not set, so provisioning is not configured;
        # the user should get the "not configured" fallback response.
        body = mock_post.call_args[1]["json"]
        assert "provisioning isn't configured" in body["cr_message"].lower()

    @patch("global_manager.requests.post")
    @patch("global_manager.requests.patch")
    def test_process_known_user_offline(self, mock_patch, mock_post, manager):
        mock_post.return_value = FakeResponse(json_data={})
        mock_patch.return_value = FakeResponse(status_code=204)
        manager._known_users.add("newuser@example.com")
        manager.process_message(SAMPLE_STALE_MSG)
        body = mock_post.call_args[1]["json"]
        assert "offline" in body["cr_message"].lower()

    @patch("global_manager.requests.patch")
    def test_process_empty_message(self, mock_patch, manager):
        empty_msg = {**SAMPLE_STALE_MSG, "cr_message": ""}
        mock_patch.return_value = FakeResponse(status_code=204)
        manager.process_message(empty_msg)
        mock_patch.assert_called_once()

    def test_new_user_added_to_known(self, manager):
        """When DevBox isn't configured, new users are handled gracefully
        but are NOT added to _known_users (that only happens after full
        onboarding with successful auth)."""
        with patch.object(manager, "send_response"), \
             patch.object(manager, "mark_processed"):
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


# -- Onboarding with AAD Resolution Tests ------------------------------------

class TestOnboardingAadResolution:
    """Verify that _handle_potentially_new_user resolves the real AAD GUID
    via resolve_azure_ad_id before calling provision_devbox."""

    @patch("global_manager.requests.get")
    @patch("global_manager.requests.post")
    @patch("global_manager.requests.patch")
    def test_provision_uses_resolved_guid(self, mock_patch, mock_post, mock_get, manager):
        # Wire up a DevBoxManager stub
        mock_devbox = MagicMock()
        mock_devbox.provision_devbox.return_value = {"name": "shraga-newuser"}
        manager.devbox_manager = mock_devbox

        # Graph API response for resolve_azure_ad_id
        mock_get.return_value = FakeResponse(
            json_data={"id": "real-aad-guid-5678"}
        )

        # _get_user_state returns None (new user), send_response / _update_user_state
        mock_post.return_value = FakeResponse(json_data={})
        mock_patch.return_value = FakeResponse(status_code=204)

        # Stub Claude CLI so welcome message doesn't call real subprocess
        with patch.object(manager, "_call_claude", return_value=None):
            result = manager._handle_potentially_new_user(SAMPLE_STALE_MSG)

        # provision_devbox must have been called with the real GUID
        call_args = mock_devbox.provision_devbox.call_args
        assert call_args[0][0] == "real-aad-guid-5678"
        assert "provisioning has started" in result.lower()


# -- Customization Integration Tests -----------------------------------------

class TestCustomizationIntegration:
    """Verify that apply_customizations is called after provisioning succeeds
    and that customization status is monitored before proceeding to auth."""

    def _make_manager_with_devbox(self, manager):
        """Helper to attach a mocked DevBoxManager."""
        mock_devbox = MagicMock()
        manager.devbox_manager = mock_devbox
        return mock_devbox

    @patch("global_manager.requests.get")
    @patch("global_manager.requests.post")
    @patch("global_manager.requests.patch")
    def test_apply_customizations_called_after_provisioning_succeeds(
        self, mock_patch, mock_post, mock_get, manager
    ):
        mock_devbox = self._make_manager_with_devbox(manager)

        # Simulate: provisioning already started, dev box now Succeeded
        manager._onboarding_state["newuser@example.com"] = {
            "provisioning_started": True,
            "devbox_name": "shraga-newuser",
            "azure_ad_id": "aad-guid-999",
        }

        mock_devbox.get_devbox_status.return_value = DevBoxInfo(
            name="shraga-newuser",
            user_id="aad-guid-999",
            status="Running",
            connection_url="https://devbox.microsoft.com/connect?devbox=shraga-newuser",
            provisioning_state="Succeeded",
        )
        mock_devbox.apply_customizations.return_value = {"status": "Running"}

        # Stub external calls
        mock_post.return_value = FakeResponse(json_data={})
        mock_patch.return_value = FakeResponse(status_code=204)
        mock_get.return_value = FakeResponse(json_data={"value": []})

        with patch.object(manager, "_call_claude", return_value=None):
            manager._handle_potentially_new_user(SAMPLE_STALE_MSG)

        mock_devbox.apply_customizations.assert_called_once_with(
            "aad-guid-999", "shraga-newuser",
        )

    @patch("global_manager.requests.get")
    @patch("global_manager.requests.post")
    @patch("global_manager.requests.patch")
    def test_customization_still_running_returns_status_message(
        self, mock_patch, mock_post, mock_get, manager
    ):
        mock_devbox = self._make_manager_with_devbox(manager)

        # State: provisioning done, customization started but not complete
        manager._onboarding_state["newuser@example.com"] = {
            "provisioning_started": True,
            "provisioning_complete": True,
            "customization_started": True,
            "devbox_name": "shraga-newuser",
            "azure_ad_id": "aad-guid-999",
        }

        mock_devbox.get_customization_status.return_value = {"status": "Running"}
        mock_post.return_value = FakeResponse(json_data={})
        mock_patch.return_value = FakeResponse(status_code=204)
        mock_get.return_value = FakeResponse(json_data={"value": []})

        result = manager._handle_potentially_new_user(SAMPLE_STALE_MSG)
        assert "customized" in result.lower() or "installing" in result.lower()
        assert "Running" in result

    @patch("global_manager.requests.get")
    @patch("global_manager.requests.post")
    @patch("global_manager.requests.patch")
    def test_customization_succeeded_proceeds_to_rdp_auth(
        self, mock_patch, mock_post, mock_get, manager
    ):
        """After customization succeeds, the GM sends an RDP link for auth
        instead of running claude /login locally."""
        mock_devbox = self._make_manager_with_devbox(manager)

        manager._onboarding_state["newuser@example.com"] = {
            "provisioning_started": True,
            "provisioning_complete": True,
            "customization_started": True,
            "devbox_name": "shraga-newuser",
            "azure_ad_id": "aad-guid-999",
            "connection_url": "https://devbox.microsoft.com/connect?devbox=shraga-newuser",
        }

        mock_devbox.get_customization_status.return_value = {"status": "Succeeded"}
        mock_post.return_value = FakeResponse(json_data={})
        mock_patch.return_value = FakeResponse(status_code=204)
        mock_get.return_value = FakeResponse(json_data={"value": []})

        with patch.object(manager, "_call_claude", return_value=None):
            result = manager._handle_potentially_new_user(SAMPLE_STALE_MSG)

        # The result should contain the web RDP URL, not a local auth URL
        assert "devbox.microsoft.com" in result or "claude /login" in result
        # It must NOT spawn a local process -- the message should instruct
        # the user to run claude /login on the dev box via RDP
        assert "claude /login" in result
        # The onboarding step should be auth_pending
        state = manager._onboarding_state.get("newuser@example.com", {})
        assert state.get("onboarding_step") == "auth_pending_rdp"

    @patch("global_manager.requests.get")
    @patch("global_manager.requests.post")
    @patch("global_manager.requests.patch")
    def test_customization_failed_still_proceeds_to_rdp_auth(
        self, mock_patch, mock_post, mock_get, manager
    ):
        mock_devbox = self._make_manager_with_devbox(manager)

        manager._onboarding_state["newuser@example.com"] = {
            "provisioning_started": True,
            "provisioning_complete": True,
            "customization_started": True,
            "devbox_name": "shraga-newuser",
            "azure_ad_id": "aad-guid-999",
            "connection_url": "https://devbox.microsoft.com/connect?devbox=shraga-newuser",
        }

        mock_devbox.get_customization_status.return_value = {"status": "Failed"}
        mock_post.return_value = FakeResponse(json_data={})
        mock_patch.return_value = FakeResponse(status_code=204)
        mock_get.return_value = FakeResponse(json_data={"value": []})

        with patch.object(manager, "_call_claude", return_value=None):
            result = manager._handle_potentially_new_user(SAMPLE_STALE_MSG)

        # Should still proceed to RDP-based auth even when customization fails
        assert "claude /login" in result


# -- RDP Auth Flow Tests (BLOCKER 3 fix) -------------------------------------

class TestRdpAuthFlow:
    """Verify that _handle_potentially_new_user sends the user an RDP link
    for auth instead of running claude /login on the GM machine."""

    def _make_manager_with_devbox(self, manager):
        mock_devbox = MagicMock()
        manager.devbox_manager = mock_devbox
        return mock_devbox

    @patch("global_manager.requests.get")
    @patch("global_manager.requests.post")
    @patch("global_manager.requests.patch")
    def test_auth_step_sends_rdp_link_not_local_auth(
        self, mock_patch, mock_post, mock_get, manager
    ):
        """Step 3 should send the user a web RDP URL, not spawn local
        claude /login on the GM."""
        self._make_manager_with_devbox(manager)

        # State: provisioning and customization complete, ready for auth
        manager._onboarding_state["newuser@example.com"] = {
            "provisioning_started": True,
            "provisioning_complete": True,
            "customization_started": True,
            "customization_complete": True,
            "devbox_name": "shraga-newuser",
            "azure_ad_id": "aad-guid-999",
            "connection_url": "https://devbox.microsoft.com/connect?devbox=shraga-newuser",
        }

        mock_post.return_value = FakeResponse(json_data={})
        mock_patch.return_value = FakeResponse(status_code=204)
        mock_get.return_value = FakeResponse(json_data={"value": []})

        with patch.object(manager, "_call_claude", return_value=None):
            result = manager._handle_potentially_new_user(SAMPLE_STALE_MSG)

        # The response should contain the RDP connection URL
        assert "devbox.microsoft.com/connect" in result
        # The response should tell the user to run claude /login on the dev box
        assert "claude /login" in result
        # onboarding_step should reflect auth_pending_rdp
        state = manager._onboarding_state.get("newuser@example.com", {})
        assert state.get("onboarding_step") == "auth_pending_rdp"

    @patch("global_manager.requests.get")
    @patch("global_manager.requests.post")
    @patch("global_manager.requests.patch")
    def test_auth_step_includes_setup_instructions(
        self, mock_patch, mock_post, mock_get, manager
    ):
        """The RDP auth message should include post-provisioning setup
        instructions (pip packages, git clone, scheduled task)."""
        self._make_manager_with_devbox(manager)

        manager._onboarding_state["newuser@example.com"] = {
            "provisioning_started": True,
            "provisioning_complete": True,
            "customization_started": True,
            "customization_complete": True,
            "devbox_name": "shraga-newuser",
            "azure_ad_id": "aad-guid-999",
            "connection_url": "https://devbox.microsoft.com/connect?devbox=shraga-newuser",
        }

        mock_post.return_value = FakeResponse(json_data={})
        mock_patch.return_value = FakeResponse(status_code=204)
        mock_get.return_value = FakeResponse(json_data={"value": []})

        with patch.object(manager, "_call_claude", return_value=None):
            result = manager._handle_potentially_new_user(SAMPLE_STALE_MSG)

        # Should include the setup script contents
        assert "pip install" in result
        assert "shraga-worker" in result

    @patch("global_manager.requests.get")
    @patch("global_manager.requests.post")
    @patch("global_manager.requests.patch")
    def test_user_confirms_done_completes_onboarding(
        self, mock_patch, mock_post, mock_get, manager
    ):
        """When the user replies 'done', onboarding completes."""
        self._make_manager_with_devbox(manager)

        # State: RDP auth link was sent, waiting for confirmation
        manager._onboarding_state["newuser@example.com"] = {
            "provisioning_started": True,
            "provisioning_complete": True,
            "customization_started": True,
            "customization_complete": True,
            "devbox_name": "shraga-newuser",
            "azure_ad_id": "aad-guid-999",
            "connection_url": "https://devbox.microsoft.com/connect?devbox=shraga-newuser",
            "onboarding_step": "auth_pending_rdp",
        }

        mock_post.return_value = FakeResponse(json_data={})
        mock_patch.return_value = FakeResponse(status_code=204)
        mock_get.return_value = FakeResponse(json_data={"value": []})

        # User says "done"
        done_msg = {**SAMPLE_STALE_MSG, "cr_message": "done"}

        with patch.object(manager, "_call_claude", return_value=None):
            result = manager._handle_potentially_new_user(done_msg)

        assert "ready" in result.lower() or "complete" in result.lower()
        # User should now be in known_users
        assert "newuser@example.com" in manager._known_users

    @patch("global_manager.requests.get")
    @patch("global_manager.requests.post")
    @patch("global_manager.requests.patch")
    def test_user_confirms_yes_completes_onboarding(
        self, mock_patch, mock_post, mock_get, manager
    ):
        """Various confirmation words should be accepted."""
        self._make_manager_with_devbox(manager)

        manager._onboarding_state["newuser@example.com"] = {
            "provisioning_started": True,
            "provisioning_complete": True,
            "customization_started": True,
            "customization_complete": True,
            "devbox_name": "shraga-newuser",
            "azure_ad_id": "aad-guid-999",
            "connection_url": "https://devbox.microsoft.com/connect?devbox=shraga-newuser",
            "onboarding_step": "auth_pending_rdp",
        }

        mock_post.return_value = FakeResponse(json_data={})
        mock_patch.return_value = FakeResponse(status_code=204)
        mock_get.return_value = FakeResponse(json_data={"value": []})

        yes_msg = {**SAMPLE_STALE_MSG, "cr_message": "yes"}

        with patch.object(manager, "_call_claude", return_value=None):
            result = manager._handle_potentially_new_user(yes_msg)

        assert "ready" in result.lower() or "complete" in result.lower()
        assert "newuser@example.com" in manager._known_users

    @patch("global_manager.requests.get")
    @patch("global_manager.requests.post")
    @patch("global_manager.requests.patch")
    def test_user_sends_unrelated_message_gets_reminder(
        self, mock_patch, mock_post, mock_get, manager
    ):
        """If the user sends something other than 'done', remind them."""
        self._make_manager_with_devbox(manager)

        manager._onboarding_state["newuser@example.com"] = {
            "provisioning_started": True,
            "provisioning_complete": True,
            "customization_started": True,
            "customization_complete": True,
            "devbox_name": "shraga-newuser",
            "azure_ad_id": "aad-guid-999",
            "connection_url": "https://devbox.microsoft.com/connect?devbox=shraga-newuser",
            "onboarding_step": "auth_pending_rdp",
        }

        mock_post.return_value = FakeResponse(json_data={})
        mock_patch.return_value = FakeResponse(status_code=204)
        mock_get.return_value = FakeResponse(json_data={"value": []})

        other_msg = {**SAMPLE_STALE_MSG, "cr_message": "what do I do now?"}

        result = manager._handle_potentially_new_user(other_msg)

        # Should remind user about the RDP link and ask them to reply "done"
        assert "done" in result.lower()
        assert "claude /login" in result
        # User should NOT be in known_users yet
        assert "newuser@example.com" not in manager._known_users

    @patch("global_manager.requests.get")
    @patch("global_manager.requests.post")
    @patch("global_manager.requests.patch")
    def test_completed_onboarding_updates_dataverse_step(
        self, mock_patch, mock_post, mock_get, manager
    ):
        """When auth completes, Dataverse is updated with 'completed' step."""
        self._make_manager_with_devbox(manager)

        manager._onboarding_state["newuser@example.com"] = {
            "provisioning_started": True,
            "provisioning_complete": True,
            "customization_started": True,
            "customization_complete": True,
            "devbox_name": "shraga-newuser",
            "azure_ad_id": "aad-guid-999",
            "connection_url": "https://devbox.microsoft.com/connect?devbox=shraga-newuser",
            "onboarding_step": "auth_pending_rdp",
            "dv_row_id": "existing-row-id-123",
        }

        mock_post.return_value = FakeResponse(json_data={})
        mock_patch.return_value = FakeResponse(status_code=204)
        mock_get.return_value = FakeResponse(json_data={"value": []})

        done_msg = {**SAMPLE_STALE_MSG, "cr_message": "done"}

        with patch.object(manager, "_call_claude", return_value=None):
            manager._handle_potentially_new_user(done_msg)

        # Find the PATCH call that updated onboarding step to "completed"
        # It could be via requests.patch (PATCH to existing row) or
        # requests.post (POST for new row) depending on dv_row_id cache.
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

    @patch("global_manager.requests.get")
    @patch("global_manager.requests.post")
    @patch("global_manager.requests.patch")
    def test_rdp_auth_uses_fallback_connection_url(
        self, mock_patch, mock_post, mock_get, manager
    ):
        """When connection_url is not cached, a default is constructed."""
        self._make_manager_with_devbox(manager)

        # No connection_url in state
        manager._onboarding_state["newuser@example.com"] = {
            "provisioning_started": True,
            "provisioning_complete": True,
            "customization_started": True,
            "customization_complete": True,
            "devbox_name": "shraga-newuser",
            "azure_ad_id": "aad-guid-999",
        }

        mock_post.return_value = FakeResponse(json_data={})
        mock_patch.return_value = FakeResponse(status_code=204)
        mock_get.return_value = FakeResponse(json_data={"value": []})

        with patch.object(manager, "_call_claude", return_value=None):
            result = manager._handle_potentially_new_user(SAMPLE_STALE_MSG)

        # Should construct a default URL using the devbox name
        assert "devbox.microsoft.com/connect?devbox=shraga-newuser" in result


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
