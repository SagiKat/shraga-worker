"""Tests for onedrive_utils: suffix-based file/folder inference (GAP-I11 / T022).

Tests:
  - test_file_link_with_extension: file paths (with extension) produce URLs without &view=0
  - test_file_link_without_local_file: file URLs are correct even when local file does not exist
  - test_folder_link_for_directory: folder paths (no extension) produce URLs with &view=0
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure the workspace root is on sys.path
REPO_ROOT = Path(__file__).parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from onedrive_utils import (
    SyncEngineMapping,
    OneDriveAccountInfo,
    local_path_to_web_url,
    _path_looks_like_file,
)


# ---------------------------------------------------------------------------
# Shared fixtures / constants
# ---------------------------------------------------------------------------

MOUNT_POINT = r"C:\Users\testuser\OneDrive - Contoso"
URL_NAMESPACE = "https://contoso-my.sharepoint.com/personal/testuser_contoso_com/Documents"
WEB_URL = "https://contoso-my.sharepoint.com/personal/testuser_contoso_com"

SYNC_MAPPING = SyncEngineMapping(
    provider_key="test-provider-key",
    mount_point=MOUNT_POINT,
    url_namespace=URL_NAMESPACE,
    library_type="mysite",
    web_url=WEB_URL,
)


def _mock_sync_engines():
    """Return a single SyncEngineMapping for testing."""
    return [SYNC_MAPPING]


def _mock_no_accounts():
    """Return empty account list."""
    return []


# ===========================================================================
# _path_looks_like_file() unit tests (supporting helper)
# ===========================================================================

class TestPathLooksLikeFile:
    """Verify the suffix-based heuristic."""

    def test_file_with_extension(self):
        assert _path_looks_like_file("result.md") is True
        assert _path_looks_like_file(r"C:\OneDrive\Sessions\TASK.md") is True
        assert _path_looks_like_file("archive.tar.gz") is True

    def test_folder_no_extension(self):
        assert _path_looks_like_file("Sessions") is False
        assert _path_looks_like_file(r"C:\OneDrive\Sessions\task1") is False

    def test_dotfile(self):
        # Dotfiles like .gitignore have stem=".gitignore" and suffix="" in
        # Python's pathlib, so they look like folders to suffix-based
        # inference. This is acceptable -- OneDrive sync scenarios primarily
        # deal with normal files (result.md, TASK.md, etc.), not dotfiles.
        assert _path_looks_like_file(".gitignore") is False
        # But files WITH a real extension after a dot-prefix are files:
        assert _path_looks_like_file(".config.json") is True

    def test_path_object(self):
        assert _path_looks_like_file(Path("report.pdf")) is True
        assert _path_looks_like_file(Path("my_folder")) is False


# ===========================================================================
# Test 1: test_file_link_with_extension
# ===========================================================================

class TestFileLinkWithExtension:
    """File paths (with extension) must produce URLs WITHOUT &view=0."""

    @patch("onedrive_utils.get_sync_engine_mappings", side_effect=_mock_sync_engines)
    @patch("onedrive_utils.get_onedrive_account_info", side_effect=_mock_no_accounts)
    def test_file_link_with_extension(self, _mock_acct, _mock_sync):
        """A path like .../Sessions/task1/result.md should open the file, not a folder."""
        local_path = MOUNT_POINT + r"\Sessions\task1\result.md"

        # Ensure the file does NOT need to exist on disk (we do not create it).
        # The old code used Path.is_file() which would return False here.
        url = local_path_to_web_url(local_path, view_in_browser=True)

        assert url is not None
        # File link should NOT have &view=0 (folder view param)
        assert "&view=0" not in url
        # Should contain the encoded file path
        assert "result.md" in url
        assert "_layouts/15/onedrive.aspx" in url

    @patch("onedrive_utils.get_sync_engine_mappings", side_effect=_mock_sync_engines)
    @patch("onedrive_utils.get_onedrive_account_info", side_effect=_mock_no_accounts)
    def test_various_extensions(self, _mock_acct, _mock_sync):
        """Multiple file types all produce file links (no &view=0)."""
        extensions = [".md", ".txt", ".py", ".json", ".pdf", ".docx", ".xlsx"]
        for ext in extensions:
            local_path = MOUNT_POINT + rf"\Sessions\task1\output{ext}"
            url = local_path_to_web_url(local_path, view_in_browser=True)
            assert url is not None, f"URL should not be None for {ext}"
            assert "&view=0" not in url, (
                f"File with extension {ext} should NOT have &view=0 but got: {url}"
            )


# ===========================================================================
# Test 2: test_file_link_without_local_file
# ===========================================================================

class TestFileLinkWithoutLocalFile:
    """File URLs must be correct even when the local file does not exist (sync race)."""

    @patch("onedrive_utils.get_sync_engine_mappings", side_effect=_mock_sync_engines)
    @patch("onedrive_utils.get_onedrive_account_info", side_effect=_mock_no_accounts)
    def test_file_link_without_local_file(self, _mock_acct, _mock_sync):
        """A non-existent local file should still get a correct file URL."""
        # This path does NOT exist on disk
        local_path = MOUNT_POINT + r"\Sessions\phantom\DELIVERABLES.md"

        assert not Path(local_path).exists(), (
            "Test precondition: the file must NOT exist on disk"
        )

        url = local_path_to_web_url(local_path, view_in_browser=True)

        assert url is not None
        # Must be a file link (no &view=0)
        assert "&view=0" not in url
        # Must contain the file name
        assert "DELIVERABLES.md" in url

    @patch("onedrive_utils.get_sync_engine_mappings", side_effect=_mock_sync_engines)
    @patch("onedrive_utils.get_onedrive_account_info", side_effect=_mock_no_accounts)
    def test_file_url_structure_without_local_file(self, _mock_acct, _mock_sync):
        """Verify the full URL structure for a non-existent file."""
        local_path = MOUNT_POINT + r"\Sessions\task42\report.pdf"

        url = local_path_to_web_url(local_path, view_in_browser=True)

        assert url is not None
        assert url.startswith(WEB_URL + "/_layouts/15/onedrive.aspx?id=")
        # The encoded path should include Documents + relative path
        assert "Documents" in url
        assert "Sessions" in url
        assert "task42" in url
        assert "report.pdf" in url


# ===========================================================================
# Test 3: test_folder_link_for_directory
# ===========================================================================

class TestFolderLinkForDirectory:
    """Folder paths (no extension) must produce URLs WITH &view=0."""

    @patch("onedrive_utils.get_sync_engine_mappings", side_effect=_mock_sync_engines)
    @patch("onedrive_utils.get_onedrive_account_info", side_effect=_mock_no_accounts)
    def test_folder_link_for_directory(self, _mock_acct, _mock_sync):
        """A path like .../Sessions/task1 (no extension) should open folder view."""
        local_path = MOUNT_POINT + r"\Sessions\task1"

        url = local_path_to_web_url(local_path, view_in_browser=True)

        assert url is not None
        # Folder link MUST have &view=0
        assert "&view=0" in url
        assert "_layouts/15/onedrive.aspx" in url

    @patch("onedrive_utils.get_sync_engine_mappings", side_effect=_mock_sync_engines)
    @patch("onedrive_utils.get_onedrive_account_info", side_effect=_mock_no_accounts)
    def test_root_folder_link(self, _mock_acct, _mock_sync):
        """The mount point itself (root folder) should also get &view=0."""
        url = local_path_to_web_url(MOUNT_POINT, view_in_browser=True)

        assert url is not None
        assert "&view=0" in url

    @patch("onedrive_utils.get_sync_engine_mappings", side_effect=_mock_sync_engines)
    @patch("onedrive_utils.get_onedrive_account_info", side_effect=_mock_no_accounts)
    def test_nested_folder_link(self, _mock_acct, _mock_sync):
        """Deeply nested folders still get &view=0."""
        local_path = MOUNT_POINT + r"\Projects\2026\Sprint3\deliverables"

        url = local_path_to_web_url(local_path, view_in_browser=True)

        assert url is not None
        assert "&view=0" in url
        assert "deliverables" in url


# ===========================================================================
# Fallback method 2: account info path (also uses suffix-based inference now)
# ===========================================================================

class TestAccountInfoFallbackFileVsFolder:
    """Method 2 (account info fallback) should also distinguish file vs folder."""

    @patch("onedrive_utils.get_sync_engine_mappings", return_value=[])
    @patch("onedrive_utils.get_onedrive_account_info")
    def test_account_fallback_file_no_view_param(self, mock_accts, _mock_sync):
        """Files via the account-info fallback should NOT have &view=0."""
        mock_accts.return_value = [
            OneDriveAccountInfo(
                account_name="Business1",
                user_folder=MOUNT_POINT,
                is_business=True,
                service_endpoint_uri="https://contoso-my.sharepoint.com/personal/testuser_contoso_com/_api",
            )
        ]

        local_path = MOUNT_POINT + r"\Sessions\task1\output.json"
        url = local_path_to_web_url(local_path, view_in_browser=True)

        assert url is not None
        assert "&view=0" not in url

    @patch("onedrive_utils.get_sync_engine_mappings", return_value=[])
    @patch("onedrive_utils.get_onedrive_account_info")
    def test_account_fallback_folder_has_view_param(self, mock_accts, _mock_sync):
        """Folders via the account-info fallback should have &view=0."""
        mock_accts.return_value = [
            OneDriveAccountInfo(
                account_name="Business1",
                user_folder=MOUNT_POINT,
                is_business=True,
                service_endpoint_uri="https://contoso-my.sharepoint.com/personal/testuser_contoso_com/_api",
            )
        ]

        local_path = MOUNT_POINT + r"\Sessions\task1"
        url = local_path_to_web_url(local_path, view_in_browser=True)

        assert url is not None
        assert "&view=0" in url
