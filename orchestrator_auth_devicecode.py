"""
DEPRECATED -- Device Code Flow authentication for Shraga Orchestrator.

This module is DEPRECATED and should NOT be used in production.

Azure Conditional Access policies in the Shraga tenant block the OAuth 2.0
Device Code grant flow.  Any attempt to authenticate via device code will be
rejected by Azure AD with an ``AADSTS50199`` (or similar) Conditional Access
error.

The recommended authentication path is:
  - For interactive/developer use:  ``az login`` + ``DefaultAzureCredential``
  - For automated/CI use:           Managed Identity or Service Principal

This file is retained for historical reference only.  It will be removed in a
future cleanup pass.
"""

from azure.identity import DeviceCodeCredential
from azure.core.credentials import AccessToken
import sys


class OrchestratorAuth:
    """
    Handles Azure authentication for the orchestrator using device code flow
    Perfect for headless/remote scenarios where the orchestrator runs without a browser
    """

    def __init__(self, tenant_id: str = None):
        """
        Initialize device code authentication

        Args:
            tenant_id: Optional Azure AD tenant ID
                      If not provided, uses common endpoint
        """
        self.tenant_id = tenant_id or "common"
        self.credential = None

    def authenticate_interactive(self) -> DeviceCodeCredential:
        """
        Authenticate using device code flow (interactive)

        Returns:
            DeviceCodeCredential that can be used for API calls

        User Experience:
            1. Script outputs: "To sign in, use a web browser to open..."
            2. User opens URL on ANY device (phone, laptop, etc.)
            3. User enters the code shown
            4. User completes sign-in
            5. Script continues automatically
        """
        print("=" * 60)
        print("ORCHESTRATOR AUTHENTICATION")
        print("=" * 60)

        # Create device code credential
        # This will automatically display the device code message
        self.credential = DeviceCodeCredential(
            tenant_id=self.tenant_id,
            # Custom callback to print instructions
            prompt_callback=self._device_code_callback
        )

        # Test the credential by getting a token
        print("\n⏳ Waiting for authentication...")
        try:
            token = self.credential.get_token("https://devcenter.azure.com/.default")
            print("✅ Authentication successful!")
            print(f"   Token expires: {token.expires_on}")
            return self.credential

        except Exception as e:
            print(f"❌ Authentication failed: {e}")
            sys.exit(1)

    def _device_code_callback(self, verification_uri: str, user_code: str, expires_in: int):
        """
        Custom callback for device code instructions

        Args:
            verification_uri: URL for user to visit
            user_code: Code for user to enter
            expires_in: Seconds until code expires
        """
        print("\n" + "=" * 60)
        print("🔐 DEVICE CODE AUTHENTICATION")
        print("=" * 60)
        print(f"\nOpen this URL on ANY device (phone, laptop, etc.):")
        print(f"   {verification_uri}")
        print(f"\nEnter this code:")
        print(f"   {user_code}")
        print(f"\nCode expires in {expires_in // 60} minutes")
        print("\nYou can do this on your phone while the orchestrator waits!")
        print("=" * 60 + "\n")

    def get_credential(self) -> DeviceCodeCredential:
        """
        Get the authenticated credential

        Returns:
            DeviceCodeCredential for API calls
        """
        if not self.credential:
            return self.authenticate_interactive()
        return self.credential


# Example usage for orchestrator startup
if __name__ == "__main__":
    print("Starting Shraga Orchestrator...")
    print("First-time setup requires authentication\n")

    # Authenticate
    auth = OrchestratorAuth()
    credential = auth.authenticate_interactive()

    print("\n✅ Orchestrator authenticated and ready!")
    print("   You can now provision Dev Boxes and manage workers")

    # Test: Get a token
    token = credential.get_token("https://devcenter.azure.com/.default")
    print(f"\n🔑 Access token acquired (expires: {token.expires_on})")

    print("\n" + "=" * 60)
    print("NEXT STEPS")
    print("=" * 60)
    print("1. Token will auto-refresh (no need to re-authenticate)")
    print("2. Run: python orchestrator.py")
    print("3. Orchestrator will use this authentication for all API calls")
    print("=" * 60)
