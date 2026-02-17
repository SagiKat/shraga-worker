"""Tests for teams_messages.py – Adaptive Card templates"""
import json
import pytest
from unittest.mock import patch, MagicMock

from teams_messages import (
    get_auth_required_card,
    get_auth_complete_card,
    get_devbox_provisioned_card,
    send_teams_message,
)


class TestGetAuthRequiredCard:
    def test_returns_valid_card_structure(self):
        card = get_auth_required_card(
            connection_url="https://devbox.example.com/connect",
            devbox_name="shraga-testuser"
        )
        assert card["type"] == "message"
        assert "attachments" in card
        assert len(card["attachments"]) == 1
        content = card["attachments"][0]["content"]
        assert content["type"] == "AdaptiveCard"
        assert content["version"] == "1.4"

    def test_contains_connection_url(self):
        card = get_auth_required_card(
            connection_url="https://devbox.example.com/connect?devbox=test",
            devbox_name="test-box"
        )
        actions = card["attachments"][0]["content"]["actions"]
        assert any("https://devbox.example.com" in a["url"] for a in actions)

    def test_contains_devbox_name(self):
        card = get_auth_required_card(
            connection_url="https://example.com",
            devbox_name="shraga-alice"
        )
        card_json = json.dumps(card)
        assert "shraga-alice" in card_json

    def test_card_is_json_serializable(self):
        card = get_auth_required_card("https://x.com", "box1")
        serialized = json.dumps(card)
        assert isinstance(serialized, str)


class TestGetAuthCompleteCard:
    def test_returns_valid_card(self):
        card = get_auth_complete_card()
        assert card["type"] == "message"
        content = card["attachments"][0]["content"]
        assert content["type"] == "AdaptiveCard"

    def test_contains_success_message(self):
        card = get_auth_complete_card()
        card_json = json.dumps(card)
        assert "Authentication Complete" in card_json

    def test_card_is_json_serializable(self):
        card = get_auth_complete_card()
        serialized = json.dumps(card)
        assert isinstance(serialized, str)


class TestGetDevboxProvisionedCard:
    def test_returns_valid_card(self):
        card = get_devbox_provisioned_card("shraga-bob", "https://devbox.example.com")
        assert card["type"] == "message"
        content = card["attachments"][0]["content"]
        assert content["type"] == "AdaptiveCard"

    def test_contains_devbox_name(self):
        card = get_devbox_provisioned_card("shraga-bob", "https://example.com")
        card_json = json.dumps(card)
        assert "shraga-bob" in card_json

    def test_contains_action_with_url(self):
        card = get_devbox_provisioned_card("box", "https://devbox.example.com/view")
        actions = card["attachments"][0]["content"]["actions"]
        assert len(actions) >= 1
        assert "https://devbox.example.com/view" in actions[0]["url"]


class TestSendTeamsMessage:
    @patch("requests.post")
    def test_sends_post_request(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        card = get_auth_complete_card()
        send_teams_message("https://webhook.example.com", card)
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args[0][0] == "https://webhook.example.com"

    @patch("requests.post")
    def test_raises_on_failure(self, mock_post):
        mock_post.return_value = MagicMock(status_code=500, text="Server Error")
        card = get_auth_complete_card()
        with pytest.raises(Exception, match="Failed to send"):
            send_teams_message("https://webhook.example.com", card)
