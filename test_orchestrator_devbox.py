"""Tests for orchestrator_devbox.py – DevBoxManager"""
import pytest
from unittest.mock import patch, MagicMock, PropertyMock

from orchestrator_devbox import DevBoxManager, DevBoxInfo


# ===========================================================================
# DevBoxManager init
# ===========================================================================

class TestDevBoxManagerInit:

    @patch("orchestrator_devbox.DefaultAzureCredential")
    def test_default_credential(self, mock_cred):
        mgr = DevBoxManager(
            devcenter_endpoint="https://dc.example.com",
            project_name="proj",
            pool_name="pool"
        )
        mock_cred.assert_called_once()
        assert mgr.devcenter_endpoint == "https://dc.example.com"
        assert mgr.project_name == "proj"
        assert mgr.pool_name == "pool"

    @patch("orchestrator_devbox.DeviceCodeCredential")
    def test_device_code_credential(self, mock_dc_cred):
        mgr = DevBoxManager(
            devcenter_endpoint="https://dc.example.com",
            project_name="proj",
            use_device_code=True
        )
        mock_dc_cred.assert_called_once()

    @patch("orchestrator_devbox.DefaultAzureCredential")
    def test_default_pool_name(self, mock_cred):
        mgr = DevBoxManager(
            devcenter_endpoint="https://dc.example.com",
            project_name="proj"
        )
        assert mgr.pool_name == "shraga-worker-pool"

    def test_external_credential_skips_default(self):
        """When an external credential is provided, DefaultAzureCredential is not used."""
        external_cred = MagicMock()
        with patch("orchestrator_devbox.DefaultAzureCredential") as mock_default:
            mgr = DevBoxManager(
                devcenter_endpoint="https://dc.example.com",
                project_name="proj",
                credential=external_cred,
            )
        mock_default.assert_not_called()
        assert mgr.credential is external_cred

    def test_external_credential_takes_precedence_over_device_code(self):
        """When both credential and use_device_code are provided, credential wins."""
        external_cred = MagicMock()
        with patch("orchestrator_devbox.DeviceCodeCredential") as mock_dc, \
             patch("orchestrator_devbox.DefaultAzureCredential") as mock_default:
            mgr = DevBoxManager(
                devcenter_endpoint="https://dc.example.com",
                project_name="proj",
                use_device_code=True,
                credential=external_cred,
            )
        mock_dc.assert_not_called()
        mock_default.assert_not_called()
        assert mgr.credential is external_cred


# ===========================================================================
# provision_devbox
# ===========================================================================

class TestProvisionDevbox:

    @patch("orchestrator_devbox.DefaultAzureCredential")
    @patch("orchestrator_devbox.requests.put")
    def test_provision_success(self, mock_put, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_put.return_value = MagicMock(
            status_code=201,
            json=lambda: {"name": "shraga-alice", "provisioningState": "Provisioning"}
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        result = mgr.provision_devbox("user-aad-id", "alice@example.com")

        assert result["name"] == "shraga-alice"
        mock_put.assert_called_once()
        # Verify URL contains user ID and devbox name
        call_url = mock_put.call_args[0][0]
        assert "user-aad-id" in call_url
        assert "shraga-alice" in call_url

    @patch("orchestrator_devbox.DefaultAzureCredential")
    @patch("orchestrator_devbox.requests.put")
    def test_provision_failure_raises(self, mock_put, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_put.return_value = MagicMock(status_code=500, text="Server Error")

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        with pytest.raises(Exception, match="Failed to provision"):
            mgr.provision_devbox("user-id", "bob@example.com")

    @patch("orchestrator_devbox.DefaultAzureCredential")
    @patch("orchestrator_devbox.requests.put")
    def test_devbox_name_derived_from_email(self, mock_put, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_put.return_value = MagicMock(
            status_code=200,
            json=lambda: {"name": "shraga-john-doe"}
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        mgr.provision_devbox("uid", "john.doe@example.com")

        call_url = mock_put.call_args[0][0]
        assert "shraga-john-doe" in call_url


# ===========================================================================
# get_devbox_status
# ===========================================================================

class TestGetDevboxStatus:

    @patch("orchestrator_devbox.DefaultAzureCredential")
    @patch("orchestrator_devbox.requests.get")
    def test_returns_devbox_info(self, mock_get, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "name": "shraga-test",
                "user": "user-id",
                "powerState": "Running",
                "provisioningState": "Succeeded"
            }
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        info = mgr.get_devbox_status("user-id", "shraga-test")

        assert isinstance(info, DevBoxInfo)
        assert info.name == "shraga-test"
        assert info.status == "Running"
        assert info.provisioning_state == "Succeeded"

    @patch("orchestrator_devbox.DefaultAzureCredential")
    @patch("orchestrator_devbox.requests.get")
    def test_status_failure_raises(self, mock_get, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_get.return_value = MagicMock(status_code=404, text="Not Found")

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        with pytest.raises(Exception, match="Failed to get Dev Box status"):
            mgr.get_devbox_status("user-id", "nonexistent")


# ===========================================================================
# get_connection_url (convenience wrapper)
# ===========================================================================

class TestGetConnectionUrl:

    @patch("orchestrator_devbox.DefaultAzureCredential")
    @patch("orchestrator_devbox.requests.get")
    def test_returns_connection_url_string(self, mock_get, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "name": "shraga-test",
                "user": "user-id",
                "powerState": "Running",
                "provisioningState": "Succeeded"
            }
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        url = mgr.get_connection_url("user-id", "shraga-test")
        assert isinstance(url, str)
        assert "devbox.microsoft.com/connect" in url
        assert "shraga-test" in url

    @patch("orchestrator_devbox.DefaultAzureCredential")
    @patch("orchestrator_devbox.requests.get")
    def test_raises_when_devbox_not_found(self, mock_get, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_get.return_value = MagicMock(status_code=404, text="Not Found")

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        with pytest.raises(Exception, match="Failed to get Dev Box status"):
            mgr.get_connection_url("user-id", "nonexistent")


# ===========================================================================
# wait_for_provisioning
# ===========================================================================

class TestWaitForProvisioning:

    @patch("orchestrator_devbox.DefaultAzureCredential")
    @patch("orchestrator_devbox.time.sleep")
    @patch("orchestrator_devbox.time.time")
    @patch("orchestrator_devbox.requests.get")
    def test_returns_when_succeeded(self, mock_get, mock_time, mock_sleep, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        # Each get_devbox_status call makes 2 requests: status + remoteConnection
        mock_get.side_effect = [
            MagicMock(status_code=200, json=lambda: {
                "name": "box", "user": "u", "powerState": "Off", "provisioningState": "Provisioning"
            }),
            MagicMock(status_code=200, json=lambda: {"webUrl": "https://rdp.example.com"}),
            MagicMock(status_code=200, json=lambda: {
                "name": "box", "user": "u", "powerState": "Running", "provisioningState": "Succeeded"
            }),
            MagicMock(status_code=200, json=lambda: {"webUrl": "https://rdp.example.com"}),
        ]
        mock_time.side_effect = [0, 10, 20, 40]

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        info = mgr.wait_for_provisioning("user-id", "box", timeout_minutes=5)
        assert info.provisioning_state == "Succeeded"

    @patch("orchestrator_devbox.DefaultAzureCredential")
    @patch("orchestrator_devbox.time.sleep")
    @patch("orchestrator_devbox.time.time")
    @patch("orchestrator_devbox.requests.get")
    def test_raises_on_failure(self, mock_get, mock_time, mock_sleep, mock_cred):
        """When provisioning fails, the exception propagates immediately
        instead of looping until timeout."""
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "name": "box", "user": "u", "powerState": "Off", "provisioningState": "Failed"
            }
        )
        mock_time.side_effect = [0, 10]

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        with pytest.raises(Exception, match="Dev Box provisioning failed"):
            mgr.wait_for_provisioning("user-id", "box", timeout_minutes=1)

    @patch("orchestrator_devbox.DefaultAzureCredential")
    @patch("orchestrator_devbox.time.sleep")
    @patch("orchestrator_devbox.time.time")
    @patch("orchestrator_devbox.requests.get")
    def test_failure_raises_immediately_without_sleeping(self, mock_get, mock_time, mock_sleep, mock_cred):
        """Provisioning failure should not call sleep before raising."""
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "name": "box", "user": "u", "powerState": "Off", "provisioningState": "Failed"
            }
        )
        mock_time.side_effect = [0, 10]

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        with pytest.raises(Exception, match="Dev Box provisioning failed"):
            mgr.wait_for_provisioning("user-id", "box", timeout_minutes=5)

        mock_sleep.assert_not_called()

    @patch("orchestrator_devbox.DefaultAzureCredential")
    @patch("orchestrator_devbox.time.sleep")
    @patch("orchestrator_devbox.time.time")
    @patch("orchestrator_devbox.requests.get")
    def test_network_error_retries_then_succeeds(self, mock_get, mock_time, mock_sleep, mock_cred):
        """Network errors from get_devbox_status are caught and retried."""
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        # First call: network error, second call: success
        mock_get.side_effect = [
            MagicMock(status_code=500, text="Server Error"),
            MagicMock(status_code=200, json=lambda: {
                "name": "box", "user": "u", "powerState": "Running", "provisioningState": "Succeeded"
            })
        ]
        mock_time.side_effect = [0, 10, 20, 30]

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        info = mgr.wait_for_provisioning("user-id", "box", timeout_minutes=5)
        assert info.provisioning_state == "Succeeded"
        # Sleep called once for the network error retry
        assert mock_sleep.call_count == 1

    @patch("orchestrator_devbox.DefaultAzureCredential")
    @patch("orchestrator_devbox.time.sleep")
    @patch("orchestrator_devbox.time.time")
    @patch("orchestrator_devbox.requests.get")
    def test_timeout_raises(self, mock_get, mock_time, mock_sleep, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "name": "box", "user": "u", "powerState": "Off", "provisioningState": "Provisioning"
            }
        )
        # Time exceeds 1 minute timeout
        mock_time.side_effect = [0, 61]

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        with pytest.raises(TimeoutError):
            mgr.wait_for_provisioning("user-id", "box", timeout_minutes=1)


# ===========================================================================
# apply_customizations
# ===========================================================================

class TestApplyCustomizations:

    @patch("orchestrator_devbox.DefaultAzureCredential")
    @patch("orchestrator_devbox.requests.put")
    def test_apply_success(self, mock_put, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_put.return_value = MagicMock(
            status_code=201,
            json=lambda: {"status": "Running", "name": "shraga-setup"}
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        result = mgr.apply_customizations("aad-guid-123", "shraga-alice")

        assert result["status"] == "Running"
        mock_put.assert_called_once()
        call_url = mock_put.call_args[0][0]
        assert "aad-guid-123" in call_url
        assert "shraga-alice" in call_url
        assert "customizationGroups/shraga-setup" in call_url

    @patch("orchestrator_devbox.DefaultAzureCredential")
    @patch("orchestrator_devbox.requests.put")
    def test_apply_sends_correct_tasks(self, mock_put, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_put.return_value = MagicMock(
            status_code=200,
            json=lambda: {"status": "Running"}
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        mgr.apply_customizations("uid", "box")

        body = mock_put.call_args[1]["json"]
        tasks = body["tasks"]
        assert len(tasks) == 3
        assert tasks[0]["name"] == "DevBox.Catalog/winget"
        assert tasks[0]["parameters"]["package"] == "Git.Git"
        assert tasks[1]["parameters"]["package"] == "Anthropic.ClaudeCode"
        assert tasks[2]["name"] == "DevBox.Catalog/choco"
        assert tasks[2]["parameters"]["package"] == "python312"

    @patch("orchestrator_devbox.DefaultAzureCredential")
    @patch("orchestrator_devbox.requests.put")
    def test_apply_uses_preview_api_version(self, mock_put, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_put.return_value = MagicMock(
            status_code=202,
            json=lambda: {"status": "Running"}
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        mgr.apply_customizations("uid", "box")

        call_params = mock_put.call_args[1]["params"]
        assert call_params["api-version"] == "2025-04-01-preview"

    @patch("orchestrator_devbox.DefaultAzureCredential")
    @patch("orchestrator_devbox.requests.put")
    def test_apply_409_already_exists_treated_as_success(self, mock_put, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_put.return_value = MagicMock(
            status_code=409,
            text="A Customization Group with name shraga-setup already exists."
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        result = mgr.apply_customizations("uid", "box")
        assert result["status"] == "AlreadyExists"

    @patch("orchestrator_devbox.DefaultAzureCredential")
    @patch("orchestrator_devbox.requests.put")
    def test_apply_failure_raises(self, mock_put, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_put.return_value = MagicMock(status_code=400, text="Bad Request")

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        with pytest.raises(Exception, match="Failed to apply customizations"):
            mgr.apply_customizations("uid", "box")


# ===========================================================================
# get_customization_status
# ===========================================================================

class TestGetCustomizationStatus:

    @patch("orchestrator_devbox.DefaultAzureCredential")
    @patch("orchestrator_devbox.requests.get")
    def test_returns_status(self, mock_get, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"status": "Succeeded", "name": "shraga-setup"}
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        result = mgr.get_customization_status("aad-guid-123", "shraga-alice")

        assert result["status"] == "Succeeded"
        call_url = mock_get.call_args[0][0]
        assert "customizationGroups/shraga-setup" in call_url
        assert "aad-guid-123" in call_url

    @patch("orchestrator_devbox.DefaultAzureCredential")
    @patch("orchestrator_devbox.requests.get")
    def test_status_running(self, mock_get, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"status": "Running", "name": "shraga-setup"}
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        result = mgr.get_customization_status("uid", "box")
        assert result["status"] == "Running"

    @patch("orchestrator_devbox.DefaultAzureCredential")
    @patch("orchestrator_devbox.requests.get")
    def test_status_uses_preview_api_version(self, mock_get, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"status": "Succeeded"}
        )

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        mgr.get_customization_status("uid", "box")

        call_params = mock_get.call_args[1]["params"]
        assert call_params["api-version"] == "2025-04-01-preview"

    @patch("orchestrator_devbox.DefaultAzureCredential")
    @patch("orchestrator_devbox.requests.get")
    def test_status_failure_raises(self, mock_get, mock_cred):
        mock_cred_inst = MagicMock()
        mock_cred_inst.get_token.return_value = MagicMock(token="fake-token")
        mock_cred.return_value = mock_cred_inst

        mock_get.return_value = MagicMock(status_code=404, text="Not Found")

        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        with pytest.raises(Exception, match="Failed to get customization status"):
            mgr.get_customization_status("uid", "box")


# ===========================================================================
# run_command_on_devbox
# ===========================================================================

class TestRunCommandOnDevbox:

    @patch("orchestrator_devbox.DefaultAzureCredential")
    def test_returns_pending_status(self, mock_cred):
        mock_cred.return_value = MagicMock()
        mgr = DevBoxManager("https://dc.example.com", "proj", "pool")
        result = mgr.run_command_on_devbox("box", "echo hello", "user-id")
        assert result["status"] == "pending"
        assert result["command"] == "echo hello"


# ===========================================================================
# DevBoxInfo dataclass
# ===========================================================================

class TestDevBoxInfo:
    def test_fields(self):
        info = DevBoxInfo(
            name="box",
            user_id="uid",
            status="Running",
            connection_url="https://example.com",
            provisioning_state="Succeeded"
        )
        assert info.name == "box"
        assert info.user_id == "uid"
        assert info.status == "Running"
        assert info.connection_url == "https://example.com"
        assert info.provisioning_state == "Succeeded"
