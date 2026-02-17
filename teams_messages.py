"""
Teams message templates for Shraga user interactions
Uses Adaptive Cards for rich interactive messages
"""

from typing import Dict, Any


def get_auth_required_card(connection_url: str, devbox_name: str) -> Dict[str, Any]:
    """
    Adaptive Card for requesting Claude Code authentication

    Args:
        connection_url: URL to connect to Dev Box
        devbox_name: Name of the Dev Box

    Returns:
        Adaptive Card JSON
    """
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "type": "AdaptiveCard",
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "Container",
                            "style": "emphasis",
                            "items": [
                                {
                                    "type": "ColumnSet",
                                    "columns": [
                                        {
                                            "type": "Column",
                                            "width": "auto",
                                            "items": [
                                                {
                                                    "type": "Image",
                                                    "url": "https://raw.githubusercontent.com/microsoft/fluentui-emoji/main/assets/Locked/3D/locked_3d.png",
                                                    "size": "Medium"
                                                }
                                            ]
                                        },
                                        {
                                            "type": "Column",
                                            "width": "stretch",
                                            "items": [
                                                {
                                                    "type": "TextBlock",
                                                    "text": "Authentication Required",
                                                    "weight": "Bolder",
                                                    "size": "Large",
                                                    "wrap": True
                                                },
                                                {
                                                    "type": "TextBlock",
                                                    "text": f"Your Dev Box worker needs Claude Code authentication",
                                                    "wrap": True,
                                                    "spacing": "Small",
                                                    "isSubtle": True
                                                }
                                            ]
                                        }
                                    ]
                                }
                            ]
                        },
                        {
                            "type": "TextBlock",
                            "text": "**What you'll see:**",
                            "weight": "Bolder",
                            "spacing": "Medium"
                        },
                        {
                            "type": "TextBlock",
                            "text": "• A fullscreen browser window with the authentication page\n• Just sign in with your Anthropic account\n• Press **ESC** when you're done to exit",
                            "wrap": True,
                            "spacing": "Small"
                        },
                        {
                            "type": "TextBlock",
                            "text": "**Steps:**",
                            "weight": "Bolder",
                            "spacing": "Medium"
                        },
                        {
                            "type": "TextBlock",
                            "text": "1. Click the button below\n2. Sign in with your Azure credentials (if prompted)\n3. You'll see the Claude Code authentication page\n4. Complete the authentication\n5. Press **ESC** to close the browser",
                            "wrap": True,
                            "spacing": "Small"
                        },
                        {
                            "type": "Container",
                            "style": "accent",
                            "items": [
                                {
                                    "type": "TextBlock",
                                    "text": "**This is a one-time setup** - future tasks will run automatically!",
                                    "wrap": True,
                                    "weight": "Bolder"
                                }
                            ],
                            "spacing": "Medium"
                        },
                        {
                            "type": "FactSet",
                            "facts": [
                                {
                                    "title": "Dev Box:",
                                    "value": devbox_name
                                },
                                {
                                    "title": "Security:",
                                    "value": "Azure AD authenticated, secure browser access"
                                }
                            ],
                            "spacing": "Small"
                        }
                    ],
                    "actions": [
                        {
                            "type": "Action.OpenUrl",
                            "title": "Open Authentication Browser",
                            "url": connection_url,
                            "style": "positive"
                        }
                    ]
                }
            }
        ]
    }


def get_auth_complete_card() -> Dict[str, Any]:
    """Adaptive Card for authentication completion notification"""
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "type": "AdaptiveCard",
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "Container",
                            "style": "good",
                            "items": [
                                {
                                    "type": "ColumnSet",
                                    "columns": [
                                        {
                                            "type": "Column",
                                            "width": "auto",
                                            "items": [
                                                {
                                                    "type": "Image",
                                                    "url": "https://raw.githubusercontent.com/microsoft/fluentui-emoji/main/assets/Check%20mark%20button/3D/check_mark_button_3d.png",
                                                    "size": "Medium"
                                                }
                                            ]
                                        },
                                        {
                                            "type": "Column",
                                            "width": "stretch",
                                            "items": [
                                                {
                                                    "type": "TextBlock",
                                                    "text": "Authentication Complete!",
                                                    "weight": "Bolder",
                                                    "size": "Large",
                                                    "wrap": True
                                                },
                                                {
                                                    "type": "TextBlock",
                                                    "text": "Your Dev Box worker is now ready to execute tasks",
                                                    "wrap": True,
                                                    "spacing": "Small"
                                                }
                                            ]
                                        }
                                    ]
                                }
                            ]
                        },
                        {
                            "type": "TextBlock",
                            "text": "Your pending task will now start execution automatically.",
                            "wrap": True,
                            "spacing": "Medium"
                        }
                    ]
                }
            }
        ]
    }


def get_devbox_provisioned_card(devbox_name: str, connection_url: str) -> Dict[str, Any]:
    """Adaptive Card for Dev Box provisioning completion"""
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "type": "AdaptiveCard",
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "Container",
                            "style": "good",
                            "items": [
                                {
                                    "type": "TextBlock",
                                    "text": "Your Dedicated Dev Box is Ready!",
                                    "weight": "Bolder",
                                    "size": "Large",
                                    "wrap": True
                                }
                            ]
                        },
                        {
                            "type": "TextBlock",
                            "text": f"Your personal Dev Box **{devbox_name}** has been provisioned and is ready to use.",
                            "wrap": True,
                            "spacing": "Medium"
                        },
                        {
                            "type": "TextBlock",
                            "text": "**Benefits:**",
                            "weight": "Bolder",
                            "spacing": "Medium"
                        },
                        {
                            "type": "TextBlock",
                            "text": "• Dedicated resources (faster task execution)\n• Pre-configured with Claude Code and development tools\n• Your personal environment",
                            "wrap": True,
                            "spacing": "Small"
                        },
                        {
                            "type": "FactSet",
                            "facts": [
                                {
                                    "title": "Dev Box Name:",
                                    "value": devbox_name
                                },
                                {
                                    "title": "Status:",
                                    "value": "Ready"
                                }
                            ],
                            "spacing": "Medium"
                        },
                        {
                            "type": "TextBlock",
                            "text": "**Next:** You'll need to authenticate Claude Code once (see next message).",
                            "wrap": True,
                            "spacing": "Medium",
                            "weight": "Bolder"
                        }
                    ],
                    "actions": [
                        {
                            "type": "Action.OpenUrl",
                            "title": "View My Dev Box",
                            "url": connection_url
                        }
                    ]
                }
            }
        ]
    }


def send_teams_message(webhook_url: str, card: Dict[str, Any]):
    """
    Send an Adaptive Card to Teams via webhook

    Args:
        webhook_url: Teams webhook URL
        card: Adaptive Card JSON
    """
    import requests

    response = requests.post(webhook_url, json=card, timeout=30)

    if response.status_code != 200:
        raise Exception(f"Failed to send Teams message: {response.status_code} {response.text}")

    print(f"✓ Teams message sent")


# Example usage
if __name__ == "__main__":
    # Example: Send auth required message
    card = get_auth_required_card(
        connection_url="https://devbox.microsoft.com/connect?devbox=shraga-user1",
        devbox_name="shraga-user1"
    )

    # Pretty print the card JSON
    import json
    print(json.dumps(card, indent=2))
