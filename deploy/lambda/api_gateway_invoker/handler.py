"""
Lambda Handler: api-gateway-invoker
=====================================
Sits between API Gateway and AgentCore Runtime.

Flow:
  Client → API Gateway (JWT Authorizer) → THIS LAMBDA → AgentCore Runtime → LangGraph Agent

Security model:
  - account_id is extracted from the validated JWT claims injected by API Gateway,
    NOT from the request body. This prevents users from querying other accounts.
  - In local/test mode (no JWT context), account_id falls back to the request body.

Environment variables (set in Lambda config):
  AGENT_RUNTIME_ARN  - Full ARN of the AgentCore Runtime
                       e.g. arn:aws:bedrock-agentcore:us-east-2:123456789012:runtime/banking_assistant-XXXXXXXX
  BEDROCK_REGION     - AWS region for Bedrock AgentCore (default: us-east-2)
                       Falls back to AWS_REGION for backward compatibility

Request (from API Gateway):
  POST /
  Headers: Authorization: Bearer <JWT>
  Body: {"prompt": "What is my current balance and minimum balance requirement?"}

Response:
  {
    "response": "Your current balance for ACC001 is ...",
    "session_id": "uuid",
    "agent_runtime_arn": "arn:...",
    "mode": "aws_bedrock_agentcore"
  }
"""

import json
import logging
import os
import uuid

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AWS_REGION = os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION", "us-east-2")
AGENT_RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")

# Lazy client — reused across warm Lambda invocations
_agentcore_client = None


def _get_client():
    global _agentcore_client
    if _agentcore_client is None:
        _agentcore_client = boto3.client(
            "bedrock-agentcore",
            region_name=AWS_REGION,
        )
    return _agentcore_client


def _extract_account_id_from_jwt(event: dict) -> str | None:
    """
    Extract account_id from the JWT claims injected by API Gateway JWT Authorizer.

    API Gateway v2 (HTTP API) injects validated claims into:
      event["requestContext"]["authorizer"]["jwt"]["claims"]

    The JWT must contain a custom claim "account_id" set during token issuance
    (e.g. via Cognito pre-token-generation Lambda trigger or custom authorizer).

    Returns None if no JWT context is present (local/direct invocation).
    """
    try:
        claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
        return claims.get("account_id") or claims.get("custom:account_id")
    except (KeyError, TypeError):
        return None


def _extract_actor_id_from_jwt(event: dict) -> str | None:
    """
    Extract a stable user identity from JWT claims for use as the AgentCore
    Memory actor_id.  Tries common claim names in order of preference:
      sub (OIDC standard subject) → user_id → email → account_id

    actor_id is the per-user memory namespace key — it must be consistent
    across sessions for the same user so memory accumulates correctly.

    Returns None if no JWT context is present.
    """
    try:
        claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
        return (
            claims.get("sub")
            or claims.get("user_id")
            or claims.get("email")
            or claims.get("account_id")
            or claims.get("custom:account_id")
        )
    except (KeyError, TypeError):
        return None


def handler(event: dict, context=None) -> dict:
    """
    API Gateway Lambda proxy integration handler.
    Accepts both direct Lambda invocations and API Gateway proxy events.
    """
    logger.info("Received event keys: %s", list(event.keys()))

    # ── Parse body from API Gateway proxy event or direct invocation ─────────
    if "body" in event:
        # API Gateway proxy integration — body is a JSON string
        try:
            body = json.loads(event["body"]) if isinstance(event["body"], str) else event["body"]
        except (json.JSONDecodeError, TypeError):
            return _api_response(400, {"error": "Invalid JSON in request body"})
    else:
        # Direct Lambda invocation (e.g. from CLI testing or agentcore invoke)
        body = event

    prompt = body.get("prompt", "").strip()
    if not prompt:
        return _api_response(400, {"error": "Missing required field: prompt"})

    if not AGENT_RUNTIME_ARN:
        return _api_response(500, {"error": "AGENT_RUNTIME_ARN environment variable not set"})

    # ── Extract account_id — from JWT claims (production) or body (local) ────
    # Security boundary: in production, account_id MUST come from the validated
    # JWT token, not from user-supplied input. This prevents IDOR attacks.
    account_id = _extract_account_id_from_jwt(event) or body.get("account_id")
    if account_id:
        logger.info("account_id resolved: %s", account_id)
    else:
        logger.info("No account_id in JWT claims or body — agent will extract from prompt")

    # ── Resolve actor_id for AgentCore Memory ────────────────────────────────
    # actor_id is the stable per-user identity used as the memory namespace key.
    # It must be consistent across sessions so conversation history accumulates.
    # Prefer JWT sub/user_id; fall back to account_id; body override for local dev.
    actor_id = _extract_actor_id_from_jwt(event) or body.get("actor_id") or account_id
    if actor_id:
        logger.info("actor_id resolved: %s", actor_id)

    # ── Generate session ID ───────────────────────────────────────────────────
    # Reuse session_id from request if provided (for multi-turn conversations)
    session_id = body.get("session_id") or str(uuid.uuid4())
    logger.info("Session ID: %s", session_id)

    # ── Invoke AgentCore Runtime ──────────────────────────────────────────────
    try:
        client = _get_client()

        # Pass account_id alongside the prompt so the agent doesn't need to
        # parse it from natural language — reduces hallucination risk.
        # Also pass actor_id so the runtime can use it as the memory namespace key.
        agentcore_payload = json.dumps({
            "prompt": prompt,
            **({"account_id": account_id} if account_id else {}),
            **({"actor_id": actor_id} if actor_id else {}),
        })

        logger.info("Invoking AgentCore Runtime: %s", AGENT_RUNTIME_ARN)
        response = client.invoke_agent_runtime(
            agentRuntimeArn=AGENT_RUNTIME_ARN,
            runtimeSessionId=session_id,
            payload=agentcore_payload,
        )

        # AgentCore returns a streaming body — read it fully
        raw_response = response["response"].read()
        agent_result = json.loads(raw_response)
        logger.info("AgentCore response received")

        # Normalise: AgentCore runtime returns {"result": "..."} per entrypoint
        answer = agent_result.get("result") or agent_result.get("response") or str(agent_result)

        result = {
            "response": answer,
            "session_id": session_id,
            "agent_runtime_arn": AGENT_RUNTIME_ARN,
            "mode": "aws_bedrock_agentcore",
        }
        return _api_response(200, result)

    except client.exceptions.ResourceNotFoundException:
        logger.error("AgentCore Runtime not found: %s", AGENT_RUNTIME_ARN)
        return _api_response(404, {"error": f"AgentCore Runtime not found: {AGENT_RUNTIME_ARN}"})
    except Exception as e:
        logger.exception("Error invoking AgentCore Runtime")
        return _api_response(500, {"error": str(e)})


def _api_response(status_code: int, body: dict) -> dict:
    """Format response for API Gateway Lambda proxy integration."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "POST,OPTIONS",
        },
        "body": json.dumps(body),
    }
