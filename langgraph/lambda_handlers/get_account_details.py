"""
Lambda Handler: get-account-details
=====================================
Loads account_data.json from the knowledge base and returns owner
information (name, email, phone, address, member_since) in Bedrock
action group format.
"""

import json
import os

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_KB_DIR = os.path.join(_BASE_DIR, "data")


def _load_json(filename: str) -> dict:
    path = os.path.join(_KB_DIR, filename)
    with open(path, "r") as f:
        return json.load(f)


def handler(event: dict, context=None) -> dict:
    """
    Expected event (Bedrock action group format):
    {
        "actionGroup": "account-actions",
        "apiPath": "/account/details",
        "httpMethod": "GET",
        "parameters": [{"name": "account_id", "value": "ACC001"}]
    }
    """
    parameters = event.get("parameters", [])
    account_id = next(
        (p["value"] for p in parameters if p["name"] == "account_id"), None
    )

    if not account_id:
        return _error_response("Missing required parameter: account_id")

    try:
        account_data = _load_json("account_data.json")
    except FileNotFoundError as e:
        return _error_response(f"Knowledge base file not found: {e}")

    account = account_data["accounts"].get(account_id)
    if not account:
        return _error_response(f"Account '{account_id}' not found.")

    result = {
        "account_id": account_id,
        "owner": account["owner"],
        "email": account["email"],
        "phone": account["phone"],
        "address": account["address"],
        "member_since": account["member_since"],
        "account_type": account["account_type"],
        "status": account["status"],
    }

    return _success_response(result)


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def _success_response(body: dict) -> dict:
    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": "account-actions",
            "apiPath": "/account/details",
            "httpMethod": "GET",
            "httpStatusCode": 200,
            "responseBody": {
                "application/json": {"body": json.dumps(body)}
            },
        },
    }


def _error_response(message: str) -> dict:
    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": "account-actions",
            "apiPath": "/account/details",
            "httpMethod": "GET",
            "httpStatusCode": 400,
            "responseBody": {
                "application/json": {"body": json.dumps({"error": message})}
            },
        },
    }


if __name__ == "__main__":
    test_event = {
        "actionGroup": "account-actions",
        "apiPath": "/account/details",
        "httpMethod": "GET",
        "parameters": [{"name": "account_id", "value": "ACC001"}],
    }
    print(json.dumps(handler(test_event), indent=2))
