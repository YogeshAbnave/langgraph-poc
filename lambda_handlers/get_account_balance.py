"""
Lambda Handler: get-account-balance
====================================
Loads account_data.json and penalty_rules.json from the knowledge base,
computes compliance status and penalty exposure, and returns a structured
response in Bedrock action group format.
"""

import json
import os

# Resolve data/ path relative to this file so it works both
# locally and when packaged as a Lambda (with data files in the same zip).
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
        "apiPath": "/account/balance",
        "httpMethod": "GET",
        "parameters": [{"name": "account_id", "value": "ACC001"}]
    }
    """
    # --- Extract account_id from parameters ---
    parameters = event.get("parameters", [])
    account_id = next(
        (p["value"] for p in parameters if p["name"] == "account_id"), None
    )

    if not account_id:
        return _error_response("Missing required parameter: account_id")

    # --- Load knowledge base ---
    try:
        account_data = _load_json("account_data.json")
        penalty_rules = _load_json("penalty_rules.json")
    except FileNotFoundError as e:
        return _error_response(f"Knowledge base file not found: {e}")

    account = account_data["accounts"].get(account_id)
    if not account:
        return _error_response(f"Account '{account_id}' not found.")

    # --- Compute compliance and penalty ---
    account_type = account["account_type"]
    current_balance = account["current_balance"]
    rules = penalty_rules["penalty_rules"].get(account_type, {})
    minimum_balance = rules.get("minimum_balance", 0)
    monthly_fee = rules.get("monthly_fee_if_below", 0)
    is_compliant = current_balance >= minimum_balance
    penalty_exposure = monthly_fee if not is_compliant else 0.0

    result = {
        "account_id": account_id,
        "current_balance": current_balance,
        "currency": "USD",
        "account_type": account_type,
        "minimum_balance_required": minimum_balance,
        "is_compliant": is_compliant,
        "penalty_exposure": penalty_exposure,
        "penalty_description": rules.get("description", ""),
    }

    return _success_response(result)


# ---------------------------------------------------------------------------
# Response helpers (Bedrock action group format)
# ---------------------------------------------------------------------------

def _success_response(body: dict) -> dict:
    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": "account-actions",
            "apiPath": "/account/balance",
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
            "apiPath": "/account/balance",
            "httpMethod": "GET",
            "httpStatusCode": 400,
            "responseBody": {
                "application/json": {"body": json.dumps({"error": message})}
            },
        },
    }


# Allow local testing
if __name__ == "__main__":
    test_event = {
        "actionGroup": "account-actions",
        "apiPath": "/account/balance",
        "httpMethod": "GET",
        "parameters": [{"name": "account_id", "value": "ACC001"}],
    }
    print(json.dumps(handler(test_event), indent=2))
