"""Tests for onedrive_utils: direct SharePoint URL generation.

Tests:
  - test_file_link_with_extension: file paths produce direct document library URLs
  - test_file_link_without_local_file: file URLs are correct even when local file does not exist
  - test_folder_link_for_directory: folder paths produce direct document library URLs
  - test_account_info_fallback: fallback method also produces direct URLs
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
    """File paths produce direct SharePoint document library URLs."""

    @patch("onedrive_utils.get_sync_engine_mappings", side_effect=_mock_sync_engines)
    @patch("onedrive_utils.get_onedrive_account_info", side_effect=_mock_no_accounts)
    def test_file_link_with_extension(self, _mock_acct, _mock_sync):
        """A path like .../Sessions/task1/result.md should produce a direct URL."""
        local_path = MOUNT_POINT + r"\Sessions\task1\result.md"

        url = local_path_to_web_url(local_path, view_in_browser=True)

        assert url is not None
        assert "result.md" in url
        # Direct URL format: https://host/personal/user/Documents/path
        assert url.startswith("https://contoso-my.sharepoint.com/personal/testuser_contoso_com/Documents/")
        # No _layouts indirection
        assert "_layouts" not in url

    @patch("onedrive_utils.get_sync_engine_mappings", side_effect=_mock_sync_engines)
    @patch("onedrive_utils.get_onedrive_account_info", side_effect=_mock_no_accounts)
    def test_various_extensions(self, _mock_acct, _mock_sync):
        """Multiple file types all produce direct URLs."""
        extensions = [".md", ".txt", ".py", ".json", ".pdf", ".docx", ".xlsx"]
        for ext in extensions:
            local_path = MOUNT_POINT + rf"\Sessions\task1\output{ext}"
            url = local_path_to_web_url(local_path, view_in_browser=True)
            assert url is not None, f"URL should not be None for {ext}"
            assert "_layouts" not in url, (
                f"File with extension {ext} should use direct URL, got: {url}"
            )


# ===========================================================================
# Test 2: test_file_link_without_local_file
# ===========================================================================

class TestFileLinkWithoutLocalFile:
    """File URLs must be correct even when the local file does not exist (sync race)."""

    @patch("onedrive_utils.get_sync_engine_mappings", side_effect=_mock_sync_engines)
    @patch("onedrive_utils.get_onedrive_account_info", side_effect=_mock_no_accounts)
    def test_file_link_without_local_file(self, _mock_acct, _mock_sync):
        """A non-existent local file should still get a correct direct URL."""
        local_path = MOUNT_POINT + r"\Sessions\phantom\DELIVERABLES.md"

        assert not Path(local_path).exists(), (
            "Test precondition: the file must NOT exist on disk"
        )

        url = local_path_to_web_url(local_path, view_in_browser=True)

        assert url is not None
        assert "DELIVERABLES.md" in url
        assert "_layouts" not in url

    @patch("onedrive_utils.get_sync_engine_mappings", side_effect=_mock_sync_engines)
    @patch("onedrive_utils.get_onedrive_account_info", side_effect=_mock_no_accounts)
    def test_file_url_structure_without_local_file(self, _mock_acct, _mock_sync):
        """Verify the full URL structure for a non-existent file."""
        local_path = MOUNT_POINT + r"\Sessions\task42\report.pdf"

        url = local_path_to_web_url(local_path, view_in_browser=True)

        assert url is not None
        # Direct URL: https://host/personal/user/Documents/Sessions/task42/report.pdf
        expected_prefix = "https://contoso-my.sharepoint.com/personal/testuser_contoso_com/Documents/"
        assert url.startswith(expected_prefix)
        assert "Sessions" in url
        assert "task42" in url
        assert "report.pdf" in url


# ===========================================================================
# Test 3: test_folder_link_for_directory
# ===========================================================================

class TestFolderLinkForDirectory:
    """Folder paths produce direct SharePoint document library URLs."""

    @patch("onedrive_utils.get_sync_engine_mappings", side_effect=_mock_sync_engines)
    @patch("onedrive_utils.get_onedrive_account_info", side_effect=_mock_no_accounts)
    def test_folder_link_for_directory(self, _mock_acct, _mock_sync):
        """A path like .../Sessions/task1 (no extension) should produce a direct URL."""
        local_path = MOUNT_POINT + r"\Sessions\task1"

        url = local_path_to_web_url(local_path, view_in_browser=True)

        assert url is not None
        assert "task1" in url
        assert "_layouts" not in url
        assert "&view=0" not in url

    @patch("onedrive_utils.get_sync_engine_mappings", side_effect=_mock_sync_engines)
    @patch("onedrive_utils.get_onedrive_account_info", side_effect=_mock_no_accounts)
    def test_root_folder_link(self, _mock_acct, _mock_sync):
        """The mount point itself (root folder) should produce a direct URL."""
        url = local_path_to_web_url(MOUNT_POINT, view_in_browser=True)

        assert url is not None
        assert "_layouts" not in url
        assert "&view=0" not in url

    @patch("onedrive_utils.get_sync_engine_mappings", side_effect=_mock_sync_engines)
    @patch("onedrive_utils.get_onedrive_account_info", side_effect=_mock_no_accounts)
    def test_nested_folder_link(self, _mock_acct, _mock_sync):
        """Deeply nested folders produce direct URLs."""
        local_path = MOUNT_POINT + r"\Projects\2026\Sprint3\deliverables"

        url = local_path_to_web_url(local_path, view_in_browser=True)

        assert url is not None
        assert "deliverables" in url
        assert "_layouts" not in url
        assert "&view=0" not in url


# ===========================================================================
# Fallback method 2: account info path
# ===========================================================================

class TestAccountInfoFallbackFileVsFolder:
    """Method 2 (account info fallback) should also produce direct URLs."""

    @patch("onedrive_utils.get_sync_engine_mappings", return_value=[])
    @patch("onedrive_utils.get_onedrive_account_info")
    def test_account_fallback_file_direct_url(self, mock_accts, _mock_sync):
        """Files via the account-info fallback should produce direct URLs."""
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
        assert "_layouts" not in url
        assert "output.json" in url

    @patch("onedrive_utils.get_sync_engine_mappings", return_value=[])
    @patch("onedrive_utils.get_onedrive_account_info")
    def test_account_fallback_folder_direct_url(self, mock_accts, _mock_sync):
        """Folders via the account-info fallback should produce direct URLs."""
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
        assert "_layouts" not in url
        assert "&view=0" not in url
        assert "task1" in url
