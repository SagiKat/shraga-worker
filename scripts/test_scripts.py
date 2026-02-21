"""
Tests for scripts/dv_helpers.py -- Shared Dataverse helper module.

All tests use unittest.mock to avoid real HTTP calls or Azure auth.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
import requests as requests_lib

# Ensure the workspace root and scripts directory are importable
WORKSPACE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, WORKSPACE_ROOT)
sys.path.insert(0, SCRIPTS_DIR)

from dv_helpers import (
    DataverseClient,
    get_auth_header,
    get_rows,
    get_row,
    create_row,
    update_row,
    delete_row,
    _build_odata_headers,
    DEFAULT_DATAVERSE_URL,
    DEFAULT_REQUEST_TIMEOUT,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FAKE_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.FAKE_TOKEN_FOR_TESTING"
FAKE_ETAG = 'W/"12345678"'
TEST_TABLE = "cr_shraga_conversations"
TEST_ROW_ID = "00000000-1111-2222-3333-444444444444"


def make_client(token: str = FAKE_TOKEN, **kwargs) -> DataverseClient:
    """Create a DataverseClient with a pre-set token so no auth calls happen."""
    return DataverseClient(token=token, **kwargs)


def make_odata_response(rows: list[dict], status_code: int = 200) -> MagicMock:
    """Create a mock requests.Response with the standard OData shape."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.content = json.dumps({"value": rows}).encode()
    mock_resp.json.return_value = {"value": rows}
    mock_resp.raise_for_status = MagicMock()
    mock_resp.headers = {}
    return mock_resp


def make_single_row_response(row: dict, status_code: int = 200) -> MagicMock:
    """Create a mock response for a single-row GET or POST."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.content = json.dumps(row).encode()
    mock_resp.json.return_value = row
    mock_resp.raise_for_status = MagicMock()
    mock_resp.headers = {}
    return mock_resp


def make_204_response(entity_id: str = "") -> MagicMock:
    """Create a mock 204 No Content response."""
    mock_resp = MagicMock()
    mock_resp.status_code = 204
    mock_resp.content = b""
    mock_resp.headers = {}
    if entity_id:
        mock_resp.headers["OData-EntityId"] = (
            f"https://org.crm3.dynamics.com/api/data/v9.2/{TEST_TABLE}({entity_id})"
        )
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


def make_patch_response(status_code: int = 204) -> MagicMock:
    """Create a mock response for PATCH requests."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.content = b""
    mock_resp.raise_for_status = MagicMock()
    mock_resp.headers = {}
    return mock_resp


# ---------------------------------------------------------------------------
# Tests: get_auth_header
# ---------------------------------------------------------------------------

class TestGetAuthHeader:
    """Tests for the get_auth_header() function."""

    def test_direct_token(self):
        """When a token is passed directly, it should be returned immediately."""
        result = get_auth_header(token="my-direct-token")
        assert result == {"Authorization": "Bearer my-direct-token"}

    @patch.dict(os.environ, {"DATAVERSE_TOKEN": "env-token-123"})
    def test_env_var_token(self):
        """When DATAVERSE_TOKEN env var is set, it should be used."""
        result = get_auth_header()
        assert result == {"Authorization": "Bearer env-token-123"}

    @patch.dict(os.environ, {}, clear=True)
    @patch("dv_helpers.subprocess.run")
    def test_az_cli_token(self, mock_run):
        """When az CLI returns a token, it should be used."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="cli-token-456\n",
        )
        # Ensure DATAVERSE_TOKEN is not set
        os.environ.pop("DATAVERSE_TOKEN", None)
        result = get_auth_header(dataverse_url="https://test.crm.dynamics.com")
        assert result == {"Authorization": "Bearer cli-token-456"}
        mock_run.assert_called_once()

    @patch.dict(os.environ, {}, clear=True)
    @patch("dv_helpers.subprocess.run")
    def test_az_cli_failure_falls_through(self, mock_run):
        """When az CLI fails, should try DefaultAzureCredential."""
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        os.environ.pop("DATAVERSE_TOKEN", None)

        # Mock DefaultAzureCredential at the azure.identity level
        # (it is imported inside get_auth_header via a local import)
        mock_cred = MagicMock()
        mock_access_token = MagicMock()
        mock_access_token.token = "default-cred-token-789"
        mock_cred.get_token.return_value = mock_access_token

        with patch("azure.identity.DefaultAzureCredential", return_value=mock_cred):
            result = get_auth_header()

        assert result == {"Authorization": "Bearer default-cred-token-789"}

    @patch.dict(os.environ, {}, clear=True)
    @patch("dv_helpers.subprocess.run")
    def test_all_methods_fail_raises(self, mock_run):
        """When all auth methods fail, RuntimeError should be raised."""
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        os.environ.pop("DATAVERSE_TOKEN", None)

        # Make DefaultAzureCredential also fail
        with patch(
            "azure.identity.DefaultAzureCredential",
            side_effect=Exception("No credentials"),
        ):
            with pytest.raises(RuntimeError, match="Could not obtain"):
                get_auth_header()


# ---------------------------------------------------------------------------
# Tests: _build_odata_headers
# ---------------------------------------------------------------------------

class TestBuildODataHeaders:
    """Tests for the internal header builder."""

    def test_basic_headers(self):
        auth = {"Authorization": "Bearer tok"}
        result = _build_odata_headers(auth)
        assert result["Authorization"] == "Bearer tok"
        assert result["Accept"] == "application/json"
        assert result["OData-MaxVersion"] == "4.0"
        assert result["OData-Version"] == "4.0"
        assert "Content-Type" not in result
        assert "If-Match" not in result

    def test_content_type(self):
        auth = {"Authorization": "Bearer tok"}
        result = _build_odata_headers(auth, content_type="application/json")
        assert result["Content-Type"] == "application/json"

    def test_etag(self):
        auth = {"Authorization": "Bearer tok"}
        result = _build_odata_headers(auth, etag=FAKE_ETAG)
        assert result["If-Match"] == FAKE_ETAG

    def test_extra_headers(self):
        auth = {"Authorization": "Bearer tok"}
        result = _build_odata_headers(
            auth, extra={"Prefer": "return=representation"}
        )
        assert result["Prefer"] == "return=representation"


# ---------------------------------------------------------------------------
# Tests: DataverseClient.get_rows (acceptance criterion #1)
# ---------------------------------------------------------------------------

class TestDvHelpersGetRows:
    """test_dv_helpers_get_rows -- acceptance criterion test."""

    @patch("dv_helpers.requests.get")
    def test_get_rows_basic(self, mock_get):
        """get_rows should return the 'value' array from the OData response."""
        sample_rows = [
            {"cr_shraga_conversationid": "id-1", "cr_status": "Unclaimed", "@odata.etag": '"e1"'},
            {"cr_shraga_conversationid": "id-2", "cr_status": "Claimed", "@odata.etag": '"e2"'},
        ]
        mock_get.return_value = make_odata_response(sample_rows)

        client = make_client()
        rows = client.get_rows(TEST_TABLE)

        assert len(rows) == 2
        assert rows[0]["cr_shraga_conversationid"] == "id-1"
        assert rows[1]["cr_status"] == "Claimed"

    @patch("dv_helpers.requests.get")
    def test_get_rows_with_filter(self, mock_get):
        """get_rows should include $filter in the URL."""
        mock_get.return_value = make_odata_response([])

        client = make_client()
        client.get_rows(
            TEST_TABLE,
            filter="cr_status eq 'Unclaimed'",
            top=5,
            orderby="createdon asc",
        )

        called_url = mock_get.call_args[0][0]
        assert "$filter=cr_status eq 'Unclaimed'" in called_url
        assert "$top=5" in called_url
        assert "$orderby=createdon asc" in called_url

    @patch("dv_helpers.requests.get")
    def test_get_rows_with_select(self, mock_get):
        """get_rows should include $select in the URL."""
        mock_get.return_value = make_odata_response([])

        client = make_client()
        client.get_rows(
            TEST_TABLE,
            select="cr_shraga_conversationid,cr_status",
        )

        called_url = mock_get.call_args[0][0]
        assert "$select=cr_shraga_conversationid,cr_status" in called_url

    @patch("dv_helpers.requests.get")
    def test_get_rows_empty_result(self, mock_get):
        """get_rows should return an empty list when no rows match."""
        mock_get.return_value = make_odata_response([])

        client = make_client()
        rows = client.get_rows(TEST_TABLE, filter="cr_status eq 'Nonexistent'")

        assert rows == []

    @patch("dv_helpers.requests.get")
    def test_get_rows_preserves_etags(self, mock_get):
        """get_rows should preserve @odata.etag in returned rows."""
        sample = [{"id": "1", "@odata.etag": FAKE_ETAG}]
        mock_get.return_value = make_odata_response(sample)

        client = make_client()
        rows = client.get_rows(TEST_TABLE)

        assert rows[0]["@odata.etag"] == FAKE_ETAG

    @patch("dv_helpers.requests.get")
    def test_get_rows_raises_on_http_error(self, mock_get):
        """get_rows should propagate HTTPError on non-2xx status."""
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.raise_for_status.side_effect = requests_lib.HTTPError(
            "401 Unauthorized", response=mock_resp
        )
        mock_get.return_value = mock_resp

        client = make_client()
        with pytest.raises(requests_lib.HTTPError):
            client.get_rows(TEST_TABLE)

    @patch("dv_helpers.requests.get")
    def test_get_rows_sends_correct_headers(self, mock_get):
        """get_rows should send Authorization plus all OData headers."""
        mock_get.return_value = make_odata_response([])

        client = make_client()
        client.get_rows(TEST_TABLE)

        actual_headers = mock_get.call_args[1]["headers"]
        assert actual_headers["Authorization"] == f"Bearer {FAKE_TOKEN}"
        assert actual_headers["Accept"] == "application/json"
        assert actual_headers["OData-MaxVersion"] == "4.0"
        assert actual_headers["OData-Version"] == "4.0"


# ---------------------------------------------------------------------------
# Tests: DataverseClient.get_row
# ---------------------------------------------------------------------------

class TestGetRow:
    """Tests for get_row (single row fetch by ID)."""

    @patch("dv_helpers.requests.get")
    def test_get_row_by_id(self, mock_get):
        """get_row should fetch a single row by its GUID."""
        row_data = {
            "cr_shraga_conversationid": TEST_ROW_ID,
            "cr_status": "Claimed",
            "@odata.etag": FAKE_ETAG,
        }
        mock_get.return_value = make_single_row_response(row_data)

        client = make_client()
        row = client.get_row(TEST_TABLE, TEST_ROW_ID)

        assert row["cr_shraga_conversationid"] == TEST_ROW_ID
        assert row["@odata.etag"] == FAKE_ETAG

        called_url = mock_get.call_args[0][0]
        assert TEST_ROW_ID in called_url

    @patch("dv_helpers.requests.get")
    def test_get_row_with_select(self, mock_get):
        """get_row should include $select when specified."""
        mock_get.return_value = make_single_row_response({"id": "x"})

        client = make_client()
        client.get_row(TEST_TABLE, TEST_ROW_ID, select="cr_status")

        called_url = mock_get.call_args[0][0]
        assert "$select=cr_status" in called_url


# ---------------------------------------------------------------------------
# Tests: DataverseClient.create_row (acceptance criterion #1)
# ---------------------------------------------------------------------------

class TestCreateRow:
    """Tests for create_row."""

    @patch("dv_helpers.requests.post")
    def test_create_row_with_representation(self, mock_post):
        """create_row should return the created row when server responds with body."""
        created_row = {
            "cr_shraga_conversationid": "new-id-123",
            "cr_name": "Test row",
            "@odata.etag": '"new-etag"',
        }
        mock_post.return_value = make_single_row_response(created_row, status_code=201)

        client = make_client()
        result = client.create_row(TEST_TABLE, {"cr_name": "Test row"})

        assert result["cr_shraga_conversationid"] == "new-id-123"
        # Verify Prefer header was sent
        actual_headers = mock_post.call_args[1]["headers"]
        assert actual_headers.get("Prefer") == "return=representation"

    @patch("dv_helpers.requests.post")
    def test_create_row_204_with_entity_id(self, mock_post):
        """create_row should extract ID from OData-EntityId header on 204."""
        mock_post.return_value = make_204_response(entity_id="extracted-id-456")

        client = make_client()
        result = client.create_row(TEST_TABLE, {"cr_name": "Test"})

        assert result is not None
        assert result["_extracted_id"] == "extracted-id-456"

    @patch("dv_helpers.requests.post")
    def test_create_row_204_no_entity_id(self, mock_post):
        """create_row should return None on 204 with no OData-EntityId."""
        mock_post.return_value = make_204_response()

        client = make_client()
        result = client.create_row(TEST_TABLE, {"cr_name": "Test"})

        assert result is None

    @patch("dv_helpers.requests.post")
    def test_create_row_sends_json_body(self, mock_post):
        """create_row should send the data dict as the JSON body."""
        mock_post.return_value = make_single_row_response({"id": "x"}, status_code=201)

        client = make_client()
        data = {"cr_name": "My task", "cr_status": "Pending"}
        client.create_row(TEST_TABLE, data)

        actual_json = mock_post.call_args[1]["json"]
        assert actual_json == data


# ---------------------------------------------------------------------------
# Tests: DataverseClient.update_row (acceptance criterion #3)
# ---------------------------------------------------------------------------

class TestDvHelpersUpdateRow:
    """test_dv_helpers_update_row -- acceptance criterion test."""

    @patch("dv_helpers.requests.patch")
    def test_update_row_success(self, mock_patch):
        """update_row should return True on successful PATCH."""
        mock_patch.return_value = make_patch_response(204)

        client = make_client()
        result = client.update_row(
            TEST_TABLE,
            TEST_ROW_ID,
            {"cr_status": "Processed"},
        )

        assert result is True

    @patch("dv_helpers.requests.patch")
    def test_update_row_with_etag(self, mock_patch):
        """update_row should send If-Match header when etag is provided."""
        mock_patch.return_value = make_patch_response(204)

        client = make_client()
        result = client.update_row(
            TEST_TABLE,
            TEST_ROW_ID,
            {"cr_status": "Claimed"},
            etag=FAKE_ETAG,
        )

        assert result is True
        actual_headers = mock_patch.call_args[1]["headers"]
        assert actual_headers["If-Match"] == FAKE_ETAG

    @patch("dv_helpers.requests.patch")
    def test_update_row_concurrency_conflict(self, mock_patch):
        """update_row should return False on HTTP 412 (ETag mismatch)."""
        mock_resp = make_patch_response(412)
        mock_resp.raise_for_status = MagicMock()  # Should NOT be called
        mock_patch.return_value = mock_resp

        client = make_client()
        result = client.update_row(
            TEST_TABLE,
            TEST_ROW_ID,
            {"cr_status": "Claimed"},
            etag=FAKE_ETAG,
        )

        assert result is False

    @patch("dv_helpers.requests.patch")
    def test_update_row_no_etag(self, mock_patch):
        """update_row without etag should NOT send If-Match header."""
        mock_patch.return_value = make_patch_response(204)

        client = make_client()
        client.update_row(TEST_TABLE, TEST_ROW_ID, {"cr_status": "Done"})

        actual_headers = mock_patch.call_args[1]["headers"]
        assert "If-Match" not in actual_headers

    @patch("dv_helpers.requests.patch")
    def test_update_row_sends_correct_url(self, mock_patch):
        """update_row should PATCH to the correct entity URL."""
        mock_patch.return_value = make_patch_response(204)

        client = make_client()
        client.update_row(TEST_TABLE, TEST_ROW_ID, {"cr_status": "Done"})

        called_url = mock_patch.call_args[0][0]
        assert TEST_TABLE in called_url
        assert TEST_ROW_ID in called_url
        assert called_url.endswith(f"{TEST_TABLE}({TEST_ROW_ID})")

    @patch("dv_helpers.requests.patch")
    def test_update_row_http_error_propagates(self, mock_patch):
        """update_row should raise HTTPError on non-412 failures."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.raise_for_status.side_effect = requests_lib.HTTPError(
            "500 Internal Server Error", response=mock_resp
        )
        mock_patch.return_value = mock_resp

        client = make_client()
        with pytest.raises(requests_lib.HTTPError):
            client.update_row(TEST_TABLE, TEST_ROW_ID, {"cr_status": "Fail"})


# ---------------------------------------------------------------------------
# Tests: DataverseClient.delete_row
# ---------------------------------------------------------------------------

class TestDeleteRow:
    """Tests for delete_row."""

    @patch("dv_helpers.requests.delete")
    def test_delete_row_success(self, mock_delete):
        """delete_row should return True on success."""
        mock_resp = MagicMock()
        mock_resp.status_code = 204
        mock_resp.raise_for_status = MagicMock()
        mock_delete.return_value = mock_resp

        client = make_client()
        result = client.delete_row(TEST_TABLE, TEST_ROW_ID)

        assert result is True
        called_url = mock_delete.call_args[0][0]
        assert TEST_ROW_ID in called_url


# ---------------------------------------------------------------------------
# Tests: DataverseClient convenience methods
# ---------------------------------------------------------------------------

class TestConvenienceMethods:
    """Tests for find_rows and upsert_row."""

    @patch("dv_helpers.requests.get")
    def test_find_rows(self, mock_get):
        """find_rows should build a filter= eq query."""
        mock_get.return_value = make_odata_response(
            [{"cr_useremail": "user@test.com"}]
        )

        client = make_client()
        rows = client.find_rows(
            "crb3b_shragausers",
            "crb3b_useremail",
            "user@test.com",
        )

        assert len(rows) == 1
        called_url = mock_get.call_args[0][0]
        assert "crb3b_useremail eq 'user@test.com'" in called_url

    @patch("dv_helpers.requests.patch")
    def test_upsert_row(self, mock_patch):
        """upsert_row should PATCH without If-Match (Dataverse UPSERT)."""
        mock_patch.return_value = make_patch_response(204)

        client = make_client()
        result = client.upsert_row(
            TEST_TABLE, TEST_ROW_ID, {"cr_status": "Processed"}
        )

        assert result is True
        actual_headers = mock_patch.call_args[1]["headers"]
        assert "If-Match" not in actual_headers


# ---------------------------------------------------------------------------
# Tests: DataverseClient configuration
# ---------------------------------------------------------------------------

class TestClientConfiguration:
    """Tests for client initialization and URL construction."""

    @patch.dict(os.environ, {}, clear=False)
    def test_default_url(self):
        """Client should use the default Dataverse URL when no env var is set."""
        # Remove DATAVERSE_URL if it's in the environment
        os.environ.pop("DATAVERSE_URL", None)
        client = make_client()
        assert client.dataverse_url == DEFAULT_DATAVERSE_URL
        assert "/api/data/v9.2" in client.api_base

    def test_custom_url(self):
        """Client should accept a custom Dataverse URL."""
        client = DataverseClient(
            dataverse_url="https://custom.crm.dynamics.com",
            token=FAKE_TOKEN,
        )
        assert client.dataverse_url == "https://custom.crm.dynamics.com"
        assert client.api_base == "https://custom.crm.dynamics.com/api/data/v9.2"

    @patch.dict(os.environ, {"DATAVERSE_URL": "https://env.crm.dynamics.com"})
    def test_env_url(self):
        """Client should read DATAVERSE_URL from environment."""
        client = DataverseClient(token=FAKE_TOKEN)
        assert client.dataverse_url == "https://env.crm.dynamics.com"

    def test_custom_timeout(self):
        """Client should accept a custom timeout."""
        client = DataverseClient(token=FAKE_TOKEN, timeout=60)
        assert client.timeout == 60

    def test_custom_api_version(self):
        """Client should accept a custom API version."""
        client = DataverseClient(token=FAKE_TOKEN, api_version="v9.1")
        assert "v9.1" in client.api_base


# ---------------------------------------------------------------------------
# Tests: Module-level convenience functions
# ---------------------------------------------------------------------------

class TestModuleLevelFunctions:
    """Tests for the module-level get_rows, create_row, update_row wrappers."""

    @patch("dv_helpers.requests.get")
    @patch("dv_helpers.get_auth_header")
    def test_module_get_rows(self, mock_auth, mock_get):
        """Module-level get_rows should work without explicit client creation."""
        mock_auth.return_value = {"Authorization": f"Bearer {FAKE_TOKEN}"}
        mock_get.return_value = make_odata_response(
            [{"id": "1", "name": "test"}]
        )

        # Reset the cached default client so our mock auth is used
        import dv_helpers
        dv_helpers._default_client = None

        rows = get_rows(TEST_TABLE, filter="cr_status eq 'Open'")
        assert len(rows) == 1

    @patch("dv_helpers.requests.patch")
    @patch("dv_helpers.get_auth_header")
    def test_module_update_row(self, mock_auth, mock_patch):
        """Module-level update_row should delegate to the default client."""
        mock_auth.return_value = {"Authorization": f"Bearer {FAKE_TOKEN}"}
        mock_patch.return_value = make_patch_response(204)

        import dv_helpers
        dv_helpers._default_client = None

        result = update_row(TEST_TABLE, TEST_ROW_ID, {"cr_status": "Done"})
        assert result is True


# ---------------------------------------------------------------------------
# Tests: ETag / Optimistic Concurrency integration scenario
# ---------------------------------------------------------------------------

class TestETagWorkflow:
    """End-to-end ETag workflow: read row, get etag, update with etag."""

    @patch("dv_helpers.requests.patch")
    @patch("dv_helpers.requests.get")
    def test_claim_message_pattern(self, mock_get, mock_patch):
        """Simulate the claim-message pattern from global_manager/task_manager.

        1. GET rows (includes @odata.etag)
        2. PATCH with If-Match to atomically claim
        """
        # Step 1: GET unclaimed messages
        messages = [
            {
                "cr_shraga_conversationid": "msg-001",
                "cr_status": "Unclaimed",
                "cr_message": "Hello",
                "@odata.etag": '"version-abc"',
            },
        ]
        mock_get.return_value = make_odata_response(messages)

        client = make_client()
        rows = client.get_rows(
            TEST_TABLE,
            filter="cr_status eq 'Unclaimed'",
            top=10,
        )

        assert len(rows) == 1
        msg = rows[0]
        etag = msg["@odata.etag"]
        row_id = msg["cr_shraga_conversationid"]

        # Step 2: PATCH to claim with ETag
        mock_patch.return_value = make_patch_response(204)

        result = client.update_row(
            TEST_TABLE,
            row_id,
            {"cr_status": "Claimed", "cr_claimed_by": "personal:user@test.com"},
            etag=etag,
        )

        assert result is True
        actual_headers = mock_patch.call_args[1]["headers"]
        assert actual_headers["If-Match"] == '"version-abc"'

    @patch("dv_helpers.requests.patch")
    @patch("dv_helpers.requests.get")
    def test_claim_loses_to_another_manager(self, mock_get, mock_patch):
        """When another manager claims first, update_row returns False (412)."""
        messages = [
            {
                "cr_shraga_conversationid": "msg-002",
                "cr_status": "Unclaimed",
                "@odata.etag": '"version-xyz"',
            },
        ]
        mock_get.return_value = make_odata_response(messages)

        client = make_client()
        rows = client.get_rows(TEST_TABLE, filter="cr_status eq 'Unclaimed'")

        msg = rows[0]

        # Simulate 412 Precondition Failed
        mock_resp = make_patch_response(412)
        mock_patch.return_value = mock_resp

        result = client.update_row(
            TEST_TABLE,
            msg["cr_shraga_conversationid"],
            {"cr_status": "Claimed"},
            etag=msg["@odata.etag"],
        )

        assert result is False


# ---------------------------------------------------------------------------
# Tests: Token caching in DataverseClient
# ---------------------------------------------------------------------------

class TestTokenCaching:
    """Tests for the client's internal token caching behavior."""

    def test_presupplied_token_does_not_expire(self):
        """A directly-supplied token should be used indefinitely."""
        client = make_client(token="static-token")
        header = client._get_auth_header()
        assert header["Authorization"] == "Bearer static-token"

        # Second call should return the same
        header2 = client._get_auth_header()
        assert header2["Authorization"] == "Bearer static-token"

    @patch("dv_helpers.get_auth_header")
    def test_auto_fetched_token_is_cached(self, mock_auth):
        """When no token is supplied, the first call should fetch and cache."""
        mock_auth.return_value = {"Authorization": "Bearer auto-fetched"}

        client = DataverseClient()
        header1 = client._get_auth_header()
        header2 = client._get_auth_header()

        assert header1["Authorization"] == "Bearer auto-fetched"
        # get_auth_header should only be called once (cached)
        assert mock_auth.call_count == 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
