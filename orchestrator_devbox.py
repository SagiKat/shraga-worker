"""
Dev Box management functions for Shraga Orchestrator
Handles provisioning, authentication, and remote command execution
"""

import requests
import time
import json
from typing import Optional, Dict, Any
from datetime import datetime, timezone, timedelta
from azure.identity import DefaultAzureCredential, DeviceCodeCredential
from dataclasses import dataclass


@dataclass
class DevBoxInfo:
    name: str
    user_id: str
    status: str
    connection_url: str
    provisioning_state: str


class DevBoxManager:
    """Manages Dev Box operations for Shraga workers"""

    def __init__(
        self,
        devcenter_endpoint: str,
        project_name: str,
        pool_name: str = "shraga-worker-pool",
        use_device_code: bool = False,
        credential=None,
    ):
        self.devcenter_endpoint = devcenter_endpoint
        self.project_name = project_name
        self.pool_name = pool_name

        # Use externally-provided credential if given (enables process-scoped auth)
        if credential is not None:
            self.credential = credential
        elif use_device_code:
            print("🔐 Using device code authentication...")
            self.credential = DeviceCodeCredential(
                tenant_id="common",
                prompt_callback=self._device_code_callback
            )
        else:
            # Try default credential chain (env vars, managed identity, Azure CLI, etc.)
            self.credential = DefaultAzureCredential()

        self.api_version = "2024-02-01"

        # Token caching
        self._token_cache = None
        self._token_expires = None

    def _device_code_callback(self, verification_uri: str, user_code: str, expires_in: int):
        """Display device code instructions"""
        print("\n" + "=" * 60)
        print("📱 AUTHENTICATION REQUIRED")
        print("=" * 60)
        print(f"\n1. Open: {verification_uri}")
        print(f"2. Enter code: {user_code}")
        print(f"3. Expires in: {expires_in // 60} minutes")
        print("\n💡 You can do this on your phone!")
        print("=" * 60 + "\n")

    def _get_token(self) -> str:
        """Get access token for Dev Center API (cached)"""
        # Return cached token if still valid
        if self._token_cache and self._token_expires:
            if datetime.now(timezone.utc) < self._token_expires:
                return self._token_cache

        token = self.credential.get_token("https://devcenter.azure.com/.default")
        self._token_cache = token.token
        self._token_expires = datetime.fromtimestamp(token.expires_on, tz=timezone.utc) - timedelta(minutes=5)
        return self._token_cache

    def _get_headers(self) -> Dict[str, str]:
        """Get HTTP headers with auth token.

        Includes a custom User-Agent because the Azure Application Gateway
        WAF in front of the Dev Center endpoint blocks the default
        ``python-requests/x.y.z`` user-agent with 403 Forbidden.
        """
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json",
            "User-Agent": "Shraga-DevBoxManager/1.0",
        }

    def provision_devbox(self, user_azure_ad_id: str, user_email: str) -> Dict[str, Any]:
        """
        Provision a Dev Box for a specific user

        Args:
            user_azure_ad_id: Azure AD object ID of the user
            user_email: User's email (for naming)

        Returns:
            Dict with provisioning details
        """
        # Generate dev box name from email
        username = user_email.split('@')[0].replace('.', '-')
        devbox_name = f"shraga-{username}"

        # API endpoint
        url = (
            f"{self.devcenter_endpoint}/projects/{self.project_name}"
            f"/users/{user_azure_ad_id}/devboxes/{devbox_name}"
        )

        # Request body
        body = {
            "poolName": self.pool_name
        }

        # Make request
        response = requests.put(
            url,
            json=body,
            headers=self._get_headers(),
            params={"api-version": self.api_version},
            timeout=30
        )

        if response.status_code in [200, 201]:
            result = response.json()
            print(f"[OK] Dev Box provisioning started: {devbox_name}")
            return result
        else:
            raise Exception(f"Failed to provision Dev Box: {response.status_code} {response.text}")

    def get_devbox_status(self, user_azure_ad_id: str, devbox_name: str) -> DevBoxInfo:
        """
        Get current status of a Dev Box

        Args:
            user_azure_ad_id: Azure AD object ID of the user
            devbox_name: Name of the Dev Box

        Returns:
            DevBoxInfo with current status
        """
        url = (
            f"{self.devcenter_endpoint}/projects/{self.project_name}"
            f"/users/{user_azure_ad_id}/devboxes/{devbox_name}"
        )

        response = requests.get(
            url,
            headers=self._get_headers(),
            params={"api-version": self.api_version},
            timeout=30
        )

        if response.status_code == 200:
            data = response.json()

            # Construct connection URL
            connection_url = f"https://devbox.microsoft.com/connect?devbox={devbox_name}"

            return DevBoxInfo(
                name=data.get("name"),
                user_id=data.get("user"),
                status=data.get("powerState", "Unknown"),
                connection_url=connection_url,
                provisioning_state=data.get("provisioningState", "Unknown")
            )
        else:
            raise Exception(f"Failed to get Dev Box status: {response.status_code} {response.text}")

    def get_connection_url(self, user_azure_ad_id: str, devbox_name: str) -> str:
        """
        Get the web RDP connection URL for a Dev Box.

        This is a convenience wrapper around get_devbox_status() that returns
        only the connection URL string, suitable for sending to the user so
        they can open a browser-based remote session.

        Args:
            user_azure_ad_id: Azure AD object ID of the user
            devbox_name: Name of the Dev Box

        Returns:
            Web RDP connection URL string
        """
        info = self.get_devbox_status(user_azure_ad_id, devbox_name)
        return info.connection_url

    def wait_for_provisioning(
        self,
        user_azure_ad_id: str,
        devbox_name: str,
        timeout_minutes: int = 15
    ) -> DevBoxInfo:
        """
        Wait for Dev Box provisioning to complete

        Args:
            user_azure_ad_id: Azure AD object ID of the user
            devbox_name: Name of the Dev Box
            timeout_minutes: Max time to wait

        Returns:
            DevBoxInfo when provisioning is complete
        """
        start_time = time.time()
        timeout_seconds = timeout_minutes * 60

        print(f"⏳ Waiting for Dev Box provisioning (timeout: {timeout_minutes}m)...")

        while True:
            # Check timeout
            elapsed = time.time() - start_time
            if elapsed > timeout_seconds:
                raise TimeoutError(f"Dev Box provisioning timed out after {timeout_minutes} minutes")

            # Get status
            try:
                info = self.get_devbox_status(user_azure_ad_id, devbox_name)
            except Exception as e:
                print(f"  Error checking status: {e}")
                time.sleep(30)
                continue

            if info.provisioning_state == "Succeeded":
                print(f"[OK] Dev Box provisioned successfully!")
                return info
            elif info.provisioning_state == "Failed":
                raise Exception("Dev Box provisioning failed")
            else:
                print(f"  Status: {info.provisioning_state} (elapsed: {int(elapsed)}s)")

            # Wait before next check
            time.sleep(30)

    def apply_customizations(
        self,
        user_azure_ad_id: str,
        devbox_name: str,
    ) -> Dict[str, Any]:
        """
        Apply customization tasks to a provisioned Dev Box via the
        Customization API (2025-04-01-preview).

        Installs Git, Claude Code, and Python 3.12 using the proven recipe.

        Args:
            user_azure_ad_id: Azure AD object ID (GUID) of the user
            devbox_name: Name of the Dev Box

        Returns:
            Dict with API response (includes operation status)
        """
        url = (
            f"{self.devcenter_endpoint}/projects/{self.project_name}"
            f"/users/{user_azure_ad_id}/devboxes/{devbox_name}"
            f"/customizationGroups/shraga-setup"
        )

        body = {
            "tasks": [
                {
                    "name": "DevBox.Catalog/winget",
                    "parameters": {"package": "Git.Git"},
                },
                {
                    "name": "DevBox.Catalog/winget",
                    "parameters": {"package": "Anthropic.ClaudeCode"},
                },
                {
                    "name": "DevBox.Catalog/choco",
                    "parameters": {"package": "python312"},
                },
            ]
        }

        response = requests.put(
            url,
            json=body,
            headers=self._get_headers(),
            params={"api-version": "2025-04-01-preview"},
            timeout=30,
        )

        if response.status_code in [200, 201, 202]:
            result = response.json()
            print(f"Customization applied to {devbox_name}")
            return result
        else:
            raise Exception(
                f"Failed to apply customizations: {response.status_code} {response.text}"
            )

    def get_customization_status(
        self,
        user_azure_ad_id: str,
        devbox_name: str,
    ) -> Dict[str, Any]:
        """
        Poll the customization group status for a Dev Box.

        Args:
            user_azure_ad_id: Azure AD object ID (GUID) of the user
            devbox_name: Name of the Dev Box

        Returns:
            Dict with 'status' key — one of 'NotStarted', 'Running',
            'Succeeded', 'Failed', 'ValidationFailed'.
        """
        url = (
            f"{self.devcenter_endpoint}/projects/{self.project_name}"
            f"/users/{user_azure_ad_id}/devboxes/{devbox_name}"
            f"/customizationGroups/shraga-setup"
        )

        response = requests.get(
            url,
            headers=self._get_headers(),
            params={"api-version": "2025-04-01-preview"},
            timeout=30,
        )

        if response.status_code == 200:
            return response.json()
        else:
            raise Exception(
                f"Failed to get customization status: {response.status_code} {response.text}"
            )

    def run_command_on_devbox(
        self,
        devbox_name: str,
        command: str,
        user_azure_ad_id: str
    ) -> Dict[str, Any]:
        """
        Run a PowerShell command on a Dev Box

        Note: This requires the Dev Box to have Azure Run Command enabled
        or use Azure DevOps Agent / custom agent for remote execution

        Args:
            devbox_name: Name of the Dev Box
            command: PowerShell command to run
            user_azure_ad_id: Azure AD object ID of the user

        Returns:
            Command execution result
        """
        # TODO: Implement remote command execution
        # Options:
        # 1. Azure Run Command (if Dev Box supports it)
        # 2. Azure DevOps Agent on Dev Box
        # 3. Custom agent polling Dataverse for commands
        # 4. SSH/WinRM (if enabled)

        print(f"Remote command execution not yet implemented")
        print(f"   Command: {command}")
        print(f"   Target: {devbox_name}")

        # For MVP, we can have a small agent on Dev Box that polls Dataverse
        # for commands to execute
        return {
            "status": "pending",
            "command": command,
            "devbox_name": devbox_name
        }

    def request_kiosk_auth(
        self,
        user_id: str,
        user_email: str,
        devbox_name: str,
        user_azure_ad_id: str
    ) -> str:
        """
        Request user to authenticate Claude Code via kiosk mode

        Args:
            user_id: Dataverse user ID
            user_email: User's email
            devbox_name: Name of the Dev Box
            user_azure_ad_id: Azure AD object ID

        Returns:
            Connection URL to send to user
        """
        print(f"🔐 Requesting kiosk authentication for {user_email}...")

        # 1. Trigger kiosk auth script on Dev Box
        # For MVP, this could be done via:
        # - Small agent on Dev Box polling Dataverse
        # - Or manual trigger by user
        # - Or scheduled task that checks a flag file

        # Command to run on Dev Box
        command = "powershell -File C:\\Dev\\shraga-worker\\kiosk-auth-helper.ps1 -Action Start"

        # Queue command for execution
        self.run_command_on_devbox(
            devbox_name=devbox_name,
            command=command,
            user_azure_ad_id=user_azure_ad_id
        )

        # 2. Get connection URL
        info = self.get_devbox_status(user_azure_ad_id, devbox_name)
        connection_url = info.connection_url

        print(f"[OK] Kiosk auth requested")
        print(f"  Connection URL: {connection_url}")

        return connection_url

    def check_claude_auth_status(
        self,
        devbox_name: str,
        user_azure_ad_id: str
    ) -> bool:
        """
        Check if Claude Code is authenticated on the Dev Box

        Args:
            devbox_name: Name of the Dev Box
            user_azure_ad_id: Azure AD object ID

        Returns:
            True if authenticated, False otherwise
        """
        # Command to check auth status
        command = "powershell -File C:\\Dev\\shraga-worker\\kiosk-auth-helper.ps1 -Action Status"

        result = self.run_command_on_devbox(
            devbox_name=devbox_name,
            command=command,
            user_azure_ad_id=user_azure_ad_id
        )

        # Parse result (this depends on how command execution is implemented)
        # For now, return False (assume not authenticated)
        return False


# Example usage
if __name__ == "__main__":
    # Configuration
    DEVCENTER_ENDPOINT = "https://your-devcenter.devcenter.azure.com"
    PROJECT_NAME = "shraga-project"
    POOL_NAME = "shraga-worker-pool"

    # Initialize manager
    manager = DevBoxManager(
        devcenter_endpoint=DEVCENTER_ENDPOINT,
        project_name=PROJECT_NAME,
        pool_name=POOL_NAME
    )

    # Example: Provision Dev Box
    user_azure_ad_id = "b08e39b4-2ac6-4465-a35e-48322efb0f98"
    user_email = "user@microsoft.com"

    # Provision
    result = manager.provision_devbox(user_azure_ad_id, user_email)
    devbox_name = result["name"]

    # Wait for provisioning
    info = manager.wait_for_provisioning(user_azure_ad_id, devbox_name)

    # Request authentication
    connection_url = manager.request_kiosk_auth(
        user_id="dataverse-user-id",
        user_email=user_email,
        devbox_name=devbox_name,
        user_azure_ad_id=user_azure_ad_id
    )

    print(f"Send this URL to user: {connection_url}")
