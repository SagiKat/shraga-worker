"""
Create the cr_shraga_conversations table in Dataverse.

Uses the Dataverse Web API to create the table and columns.
Requires: az login (DefaultAzureCredential)
"""
import requests
import json
import os
from datetime import datetime, timezone, timedelta
from azure.identity import DefaultAzureCredential
from azure.core.credentials import AccessToken

DATAVERSE_URL = os.environ.get("DATAVERSE_URL", "https://org3e79cdb1.crm3.dynamics.com")
DATAVERSE_API = f"{DATAVERSE_URL}/api/data/v9.2"
REQUEST_TIMEOUT = 30


def get_token():
    cred = DefaultAzureCredential()
    token = cred.get_token(f"{DATAVERSE_URL}/.default")
    return token.token


def headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "OData-MaxVersion": "4.0",
        "OData-Version": "4.0",
    }


def create_table(token):
    """Create the conversations table via EntityDefinitions API."""
    url = f"{DATAVERSE_API}/EntityDefinitions"
    body = {
        "@odata.type": "Microsoft.Dynamics.CRM.EntityMetadata",
        "SchemaName": "cr_shraga_conversation",
        "DisplayName": {
            "@odata.type": "Microsoft.Dynamics.CRM.Label",
            "LocalizedLabels": [{"@odata.type": "Microsoft.Dynamics.CRM.LocalizedLabel",
                                  "Label": "Shraga Conversation", "LanguageCode": 1033}],
        },
        "DisplayCollectionName": {
            "@odata.type": "Microsoft.Dynamics.CRM.Label",
            "LocalizedLabels": [{"@odata.type": "Microsoft.Dynamics.CRM.LocalizedLabel",
                                  "Label": "Shraga Conversations", "LanguageCode": 1033}],
        },
        "Description": {
            "@odata.type": "Microsoft.Dynamics.CRM.Label",
            "LocalizedLabels": [{"@odata.type": "Microsoft.Dynamics.CRM.LocalizedLabel",
                                  "Label": "Message bus between MCS bot and task managers", "LanguageCode": 1033}],
        },
        "HasNotes": False,
        "HasActivities": False,
        "OwnershipType": "OrganizationOwned",
        "IsActivity": False,
        "PrimaryNameAttribute": "cr_name",
        "Attributes": [
            {
                "AttributeType": "String",
                "SchemaName": "cr_name",
                "MaxLength": 200,
                "IsPrimaryName": True,
                "DisplayName": {"@odata.type": "Microsoft.Dynamics.CRM.Label",
                                "LocalizedLabels": [{"@odata.type": "Microsoft.Dynamics.CRM.LocalizedLabel",
                                                      "Label": "Name", "LanguageCode": 1033}]},
                "@odata.type": "Microsoft.Dynamics.CRM.StringAttributeMetadata",
            },
            {
                "AttributeType": "String",
                "SchemaName": "cr_useremail",
                "MaxLength": 200,
                "DisplayName": {"@odata.type": "Microsoft.Dynamics.CRM.Label",
                                "LocalizedLabels": [{"@odata.type": "Microsoft.Dynamics.CRM.LocalizedLabel",
                                                      "Label": "User Email", "LanguageCode": 1033}]},
                "@odata.type": "Microsoft.Dynamics.CRM.StringAttributeMetadata",
            },
            {
                "AttributeType": "String",
                "SchemaName": "cr_mcs_conversation_id",
                "MaxLength": 500,
                "DisplayName": {"@odata.type": "Microsoft.Dynamics.CRM.Label",
                                "LocalizedLabels": [{"@odata.type": "Microsoft.Dynamics.CRM.LocalizedLabel",
                                                      "Label": "MCS Conversation ID", "LanguageCode": 1033}]},
                "@odata.type": "Microsoft.Dynamics.CRM.StringAttributeMetadata",
            },
            {
                "AttributeType": "Memo",
                "SchemaName": "cr_message",
                "MaxLength": 100000,
                "DisplayName": {"@odata.type": "Microsoft.Dynamics.CRM.Label",
                                "LocalizedLabels": [{"@odata.type": "Microsoft.Dynamics.CRM.LocalizedLabel",
                                                      "Label": "Message", "LanguageCode": 1033}]},
                "@odata.type": "Microsoft.Dynamics.CRM.MemoAttributeMetadata",
            },
            {
                "AttributeType": "Integer",
                "SchemaName": "cr_direction",
                "MinValue": 1,
                "MaxValue": 2,
                "DisplayName": {"@odata.type": "Microsoft.Dynamics.CRM.Label",
                                "LocalizedLabels": [{"@odata.type": "Microsoft.Dynamics.CRM.LocalizedLabel",
                                                      "Label": "Direction", "LanguageCode": 1033}]},
                "Description": {"@odata.type": "Microsoft.Dynamics.CRM.Label",
                                "LocalizedLabels": [{"@odata.type": "Microsoft.Dynamics.CRM.LocalizedLabel",
                                                      "Label": "1=Inbound, 2=Outbound", "LanguageCode": 1033}]},
                "@odata.type": "Microsoft.Dynamics.CRM.IntegerAttributeMetadata",
            },
            {
                "AttributeType": "Integer",
                "SchemaName": "cr_status",
                "MinValue": 1,
                "MaxValue": 4,
                "DisplayName": {"@odata.type": "Microsoft.Dynamics.CRM.Label",
                                "LocalizedLabels": [{"@odata.type": "Microsoft.Dynamics.CRM.LocalizedLabel",
                                                      "Label": "Status", "LanguageCode": 1033}]},
                "Description": {"@odata.type": "Microsoft.Dynamics.CRM.Label",
                                "LocalizedLabels": [{"@odata.type": "Microsoft.Dynamics.CRM.LocalizedLabel",
                                                      "Label": "1=Unclaimed, 2=Claimed, 3=Processed, 4=Delivered", "LanguageCode": 1033}]},
                "@odata.type": "Microsoft.Dynamics.CRM.IntegerAttributeMetadata",
            },
            {
                "AttributeType": "String",
                "SchemaName": "cr_claimed_by",
                "MaxLength": 200,
                "DisplayName": {"@odata.type": "Microsoft.Dynamics.CRM.Label",
                                "LocalizedLabels": [{"@odata.type": "Microsoft.Dynamics.CRM.LocalizedLabel",
                                                      "Label": "Claimed By", "LanguageCode": 1033}]},
                "@odata.type": "Microsoft.Dynamics.CRM.StringAttributeMetadata",
            },
            {
                "AttributeType": "String",
                "SchemaName": "cr_in_reply_to",
                "MaxLength": 100,
                "DisplayName": {"@odata.type": "Microsoft.Dynamics.CRM.Label",
                                "LocalizedLabels": [{"@odata.type": "Microsoft.Dynamics.CRM.LocalizedLabel",
                                                      "Label": "In Reply To", "LanguageCode": 1033}]},
                "@odata.type": "Microsoft.Dynamics.CRM.StringAttributeMetadata",
            },
        ],
    }

    print(f"Creating table at {url}...")
    resp = requests.post(url, headers=headers(token), json=body, timeout=60)
    if resp.status_code in (200, 201, 204):
        print(f"Table created successfully!")
        return True
    else:
        print(f"Failed: {resp.status_code}")
        print(resp.text[:500])
        return False


if __name__ == "__main__":
    print("Getting token...")
    token = get_token()
    print("Creating conversations table...")
    create_table(token)
