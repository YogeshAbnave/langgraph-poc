"""
Banking Tools
=============
LangChain @tool definitions that the LLM can call.

Each tool invokes the corresponding Lambda function using the Bedrock
action group payload format defined in LangGraph.md.

In production:  set USE_LAMBDA=true and provide the correct Lambda names.
In local/test:  USE_LAMBDA defaults to false and the Lambda handlers are
                imported and called directly (no AWS Lambda needed).
"""

import json
import os
import boto3
from langchain_core.tools import tool

# ---------------------------------------------------------------------------
# Execution mode: local (direct import) vs AWS Lambda invocation
# ---------------------------------------------------------------------------
USE_LAMBDA = os.environ.get("USE_LAMBDA", "false").lower() == "true"
LAMBDA_BALANCE_FN  = os.environ.get("LAMBDA_BALANCE_FN",  "get-account-balance")
LAMBDA_DETAILS_FN  = os.environ.get("LAMBDA_DETAILS_FN",  "get-account-details")
AWS_REGION         = os.environ.get("AWS_REGION", "us-east-2")

_lambda_client = None  # lazy-initialised only when USE_LAMBDA=true


def _get_lambda_client():
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda", region_name=AWS_REGION)
    return _lambda_client


def _invoke(function_name: str, payload: dict) -> dict:
    """
    Dispatch to Lambda (production) or local handler (development).
    Returns the parsed inner body dict from the Bedrock response envelope.
    """
    if USE_LAMBDA:
        client = _get_lambda_client()
        response = client.invoke(
            FunctionName=function_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode(),
        )
        raw = json.loads(response["Payload"].read())
    else:
        # Local: import and call the handler directly
        if function_name == LAMBDA_BALANCE_FN:
            from lambda_handlers.get_account_balance import handler
        else:
            from lambda_handlers.get_account_details import handler
        raw = handler(payload)

    # Unwrap Bedrock action group envelope
    try:
        body_str = (
            raw["response"]["responseBody"]["application/json"]["body"]
        )
        return json.loads(body_str)
    except (KeyError, TypeError, json.JSONDecodeError):
        return raw  # return as-is if envelope is missing


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@tool
def get_account_balance(account_id: str) -> dict:
    """
    Retrieve the current account balance, minimum balance requirement,
    compliance status, and penalty exposure for a given account.

    Args:
        account_id: The unique account identifier (e.g. 'ACC001').

    Returns:
        dict with keys: account_id, current_balance, currency, account_type,
        minimum_balance_required, is_compliant, penalty_exposure,
        penalty_description.
    """
    payload = {
        "actionGroup": "account-actions",
        "apiPath": "/account/balance",
        "httpMethod": "GET",
        "parameters": [{"name": "account_id", "value": account_id}],
    }
    return _invoke(LAMBDA_BALANCE_FN, payload)


@tool
def get_account_details(account_id: str) -> dict:
    """
    Retrieve owner information for a given account: name, email, phone,
    address, and member-since date.

    Args:
        account_id: The unique account identifier (e.g. 'ACC001').

    Returns:
        dict with keys: account_id, owner, email, phone, address,
        member_since, account_type, status.
    """
    payload = {
        "actionGroup": "account-actions",
        "apiPath": "/account/details",
        "httpMethod": "GET",
        "parameters": [{"name": "account_id", "value": account_id}],
    }
    return _invoke(LAMBDA_DETAILS_FN, payload)
