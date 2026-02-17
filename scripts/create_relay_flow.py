"""
Create the ShragaRelay flow in Power Automate via Dataverse API.

This flow is called by the Copilot Studio bot topic to relay messages
between the user and task managers via the conversations table.

Flow logic:
1. Receive userEmail, conversationId, messageText from bot
2. Create inbound row in cr_shraga_conversations
3. Poll for outbound response (Do Until loop, 3s intervals, 5min timeout)
4. Mark response as delivered
5. Return response text to bot
"""
import requests
import json
import os
from azure.identity import DefaultAzureCredential

DATAVERSE_URL = os.environ.get("DATAVERSE_URL", "https://org3e79cdb1.crm3.dynamics.com")
DATAVERSE_API = f"{DATAVERSE_URL}/api/data/v9.2"

# Connection reference from existing flows in this environment
DATAVERSE_CONN_REF = "copilots_header_4bc17.shared_commondataserviceforapps.57aef69c3763444e8cfb3b0b5ba18fea"
DATAVERSE_CONN_NAME = "57aef69c3763444e8cfb3b0b5ba18fea"


def get_token():
    cred = DefaultAzureCredential()
    return cred.get_token(f"{DATAVERSE_URL}/.default").token


def build_flow_definition():
    """Build the ShragaRelay flow definition (clientdata)."""

    # The $filter expression references the inbound row ID
    inbound_id_expr = "outputs('Add_Inbound_Row')?['body/cr_shraga_conversationid']"
    filter_expr = f"cr_in_reply_to eq '@{{{inbound_id_expr}}}' and cr_direction eq 2"

    return {
        "properties": {
            "connectionReferences": {
                "shared_commondataserviceforapps": {
                    "impersonation": {},
                    "runtimeSource": "embedded",
                    "connection": {
                        "name": DATAVERSE_CONN_NAME,
                        "connectionReferenceLogicalName": DATAVERSE_CONN_REF
                    },
                    "api": {
                        "name": "shared_commondataserviceforapps"
                    }
                }
            },
            "definition": {
                "$schema": "https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#",
                "contentVersion": "1.0.0.0",
                "parameters": {
                    "$connections": {"defaultValue": {}, "type": "Object"},
                    "$authentication": {"defaultValue": {}, "type": "SecureObject"}
                },
                "triggers": {
                    "manual": {
                        "metadata": {"operationMetadataId": "a0000001-0000-0000-0000-000000000001"},
                        "type": "Request",
                        "kind": "Skills",
                        "inputs": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "text": {
                                        "description": "User email address",
                                        "title": "userEmail",
                                        "type": "string",
                                        "x-ms-content-hint": "TEXT",
                                        "x-ms-dynamically-added": True
                                    },
                                    "text_1": {
                                        "description": "MCS conversation ID",
                                        "title": "conversationId",
                                        "type": "string",
                                        "x-ms-content-hint": "TEXT",
                                        "x-ms-dynamically-added": True
                                    },
                                    "text_2": {
                                        "description": "The user message text",
                                        "title": "messageText",
                                        "type": "string",
                                        "x-ms-content-hint": "TEXT",
                                        "x-ms-dynamically-added": True
                                    }
                                },
                                "required": ["text", "text_1", "text_2"]
                            }
                        }
                    }
                },
                "actions": {
                    # Step 1: Write inbound message to conversations table
                    "Add_Inbound_Row": {
                        "runAfter": {},
                        "metadata": {"operationMetadataId": "a0000001-0000-0000-0000-000000000002"},
                        "type": "OpenApiConnection",
                        "inputs": {
                            "parameters": {
                                "entityName": "cr_shraga_conversations",
                                "item/cr_name": "@take(coalesce(triggerBody()?['text_2'],'(empty)'), 200)",
                                "item/cr_useremail": "@triggerBody()?['text']",
                                "item/cr_mcs_conversation_id": "@triggerBody()?['text_1']",
                                "item/cr_message": "@triggerBody()?['text_2']",
                                "item/cr_direction": 1,
                                "item/cr_status": 1
                            },
                            "host": {
                                "apiId": "/providers/Microsoft.PowerApps/apis/shared_commondataserviceforapps",
                                "connectionName": "shared_commondataserviceforapps",
                                "operationId": "CreateRecord"
                            },
                            "authentication": "@parameters('$authentication')"
                        }
                    },

                    # Step 2: Initialize variables for polling
                    "Initialize_ResponseFound": {
                        "runAfter": {"Add_Inbound_Row": ["Succeeded"]},
                        "metadata": {"operationMetadataId": "a0000001-0000-0000-0000-000000000003"},
                        "type": "InitializeVariable",
                        "inputs": {
                            "variables": [{"name": "ResponseFound", "type": "boolean", "value": False}]
                        }
                    },
                    "Initialize_ResponseText": {
                        "runAfter": {"Initialize_ResponseFound": ["Succeeded"]},
                        "metadata": {"operationMetadataId": "a0000001-0000-0000-0000-000000000004"},
                        "type": "InitializeVariable",
                        "inputs": {
                            "variables": [{"name": "ResponseText", "type": "string", "value": ""}]
                        }
                    },
                    "Initialize_ResponseRowId": {
                        "runAfter": {"Initialize_ResponseText": ["Succeeded"]},
                        "metadata": {"operationMetadataId": "a0000001-0000-0000-0000-000000000005"},
                        "type": "InitializeVariable",
                        "inputs": {
                            "variables": [{"name": "ResponseRowId", "type": "string", "value": ""}]
                        }
                    },

                    # Step 3: Poll for outbound response
                    "Do_Until_Response": {
                        "runAfter": {"Initialize_ResponseRowId": ["Succeeded"]},
                        "metadata": {"operationMetadataId": "a0000001-0000-0000-0000-000000000006"},
                        "type": "Until",
                        "expression": "@equals(variables('ResponseFound'), true)",
                        "limit": {"count": 100, "timeout": "PT5M"},
                        "actions": {
                            "Delay_3s": {
                                "runAfter": {},
                                "metadata": {"operationMetadataId": "a0000001-0000-0000-0000-000000000007"},
                                "type": "Wait",
                                "inputs": {"interval": {"count": 3, "unit": "Second"}}
                            },
                            "List_Response_Rows": {
                                "runAfter": {"Delay_3s": ["Succeeded"]},
                                "metadata": {"operationMetadataId": "a0000001-0000-0000-0000-000000000008"},
                                "type": "OpenApiConnection",
                                "inputs": {
                                    "parameters": {
                                        "entityName": "cr_shraga_conversations",
                                        "$filter": filter_expr,
                                        "$top": 1
                                    },
                                    "host": {
                                        "apiId": "/providers/Microsoft.PowerApps/apis/shared_commondataserviceforapps",
                                        "connectionName": "shared_commondataserviceforapps",
                                        "operationId": "ListRecords"
                                    },
                                    "authentication": "@parameters('$authentication')"
                                }
                            },
                            "Check_If_Found": {
                                "runAfter": {"List_Response_Rows": ["Succeeded"]},
                                "metadata": {"operationMetadataId": "a0000001-0000-0000-0000-000000000009"},
                                "type": "If",
                                "expression": {
                                    "and": [{"greater": ["@length(outputs('List_Response_Rows')?['body/value'])", 0]}]
                                },
                                "actions": {
                                    "Set_ResponseFound": {
                                        "runAfter": {},
                                        "type": "SetVariable",
                                        "inputs": {"name": "ResponseFound", "value": True}
                                    },
                                    "Set_ResponseText": {
                                        "runAfter": {"Set_ResponseFound": ["Succeeded"]},
                                        "type": "SetVariable",
                                        "inputs": {
                                            "name": "ResponseText",
                                            "value": "@{first(outputs('List_Response_Rows')?['body/value'])?['cr_message']}"
                                        }
                                    },
                                    "Set_ResponseRowId": {
                                        "runAfter": {"Set_ResponseText": ["Succeeded"]},
                                        "type": "SetVariable",
                                        "inputs": {
                                            "name": "ResponseRowId",
                                            "value": "@{first(outputs('List_Response_Rows')?['body/value'])?['cr_shraga_conversationid']}"
                                        }
                                    }
                                },
                                "else": {"actions": {}}
                            }
                        }
                    },

                    # Step 4: Mark response as delivered
                    "Mark_Response_Delivered": {
                        "runAfter": {"Do_Until_Response": ["Succeeded"]},
                        "metadata": {"operationMetadataId": "a0000001-0000-0000-0000-000000000010"},
                        "type": "OpenApiConnection",
                        "inputs": {
                            "parameters": {
                                "entityName": "cr_shraga_conversations",
                                "recordId": "@variables('ResponseRowId')",
                                "item/cr_status": 4
                            },
                            "host": {
                                "apiId": "/providers/Microsoft.PowerApps/apis/shared_commondataserviceforapps",
                                "connectionName": "shared_commondataserviceforapps",
                                "operationId": "UpdateRecord"
                            },
                            "authentication": "@parameters('$authentication')"
                        }
                    },

                    # Step 5: Return response text to bot
                    "Respond_to_the_agent": {
                        "runAfter": {"Mark_Response_Delivered": ["Succeeded"]},
                        "metadata": {"operationMetadataId": "a0000001-0000-0000-0000-000000000011"},
                        "type": "Response",
                        "kind": "Skills",
                        "inputs": {
                            "statusCode": 200,
                            "body": {
                                "responseText": "@{variables('ResponseText')}"
                            },
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "responseText": {
                                        "title": "responseText",
                                        "x-ms-dynamically-added": True,
                                        "type": "string"
                                    }
                                }
                            }
                        }
                    }
                },
                "outputs": {}
            }
        },
        "schemaVersion": "1.0.0.0"
    }


def create_flow(token):
    """Create the ShragaRelay flow via Dataverse workflows table."""
    flow_def = build_flow_definition()
    payload = {
        "category": 5,
        "name": "ShragaRelay",
        "type": 1,
        "description": "MCS Relay: writes inbound messages to conversations table, polls for outbound response, returns to bot.",
        "primaryentity": "none",
        "clientdata": json.dumps(flow_def)
    }

    url = f"{DATAVERSE_API}/workflows"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "OData-MaxVersion": "4.0",
        "OData-Version": "4.0",
    }

    print(f"Creating ShragaRelay flow...")
    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    print(f"Status: {resp.status_code}")

    if resp.status_code in (200, 201, 204):
        print("Flow created successfully!")
        entity_id = resp.headers.get("OData-EntityId", "")
        if entity_id:
            # Extract GUID from the entity URL
            flow_id = entity_id.split("(")[-1].rstrip(")")
            print(f"Flow ID: {flow_id}")
            return flow_id
    else:
        print(f"Error: {resp.text[:1000]}")
        return None


def activate_flow(token, flow_id):
    """Activate the flow by setting statecode=1."""
    url = f"{DATAVERSE_API}/workflows({flow_id})"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "OData-MaxVersion": "4.0",
        "OData-Version": "4.0",
    }
    payload = {"statecode": 1}
    print(f"Activating flow {flow_id}...")
    resp = requests.patch(url, headers=headers, json=payload, timeout=30)
    print(f"Activate status: {resp.status_code}")
    if resp.status_code in (200, 204):
        print("Flow activated!")
        return True
    else:
        print(f"Error: {resp.text[:500]}")
        return False


if __name__ == "__main__":
    print("Getting token...")
    token = get_token()

    flow_id = create_flow(token)
    if flow_id:
        activate_flow(token, flow_id)
