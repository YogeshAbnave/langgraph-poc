"""
Lambda Handler: api-gateway-invoker
=====================================
Sits between API Gateway and AgentCore Runtime (Supervisor Agent).

Flow:
  Client → API Gateway (JWT Authorizer) → THIS LAMBDA → Supervisor Agent → Worker Agent

Security model:
  - account_id is extracted from the validated JWT claims injected by API Gateway.
  - The user's Bearer token is forwarded to the Supervisor so its Cognito JWT
    inbound auth can validate it. This is the correct pattern when the downstream
    runtime has its own JWT authorizer.

Environment variables:
  AGENT_RUNTIME_ARN  - Full ARN of the Supervisor AgentCore Runtime
  BEDROCK_REGION     - AWS region (default: us-east-2)
"""

import json
import logging
import os
import uuid

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AWS_REGION        = os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION", "us-east-2")
AGENT_RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")

_agentcore_client = None


def _get_client():
    global _agentcore_client
    if _agentcore_client is None:
        _agentcore_client = boto3.client("bedrock-agentcore", region_name=AWS_REGION)
    return _agentcore_client


def _extract_bearer_token(event: dict) -> str | None:
    """
    Extract the raw Bearer token from the Authorization header.
    API Gateway v2 injects the original Authorization header into the event.
    We forward it to the Supervisor so its Cognito JWT auth can validate it.
    """
    try:
        auth_header = (
            event.get("headers", {}).get("authorization")
            or event.get("headers", {}).get("Authorization")
            or ""
        )
        if auth_header.lower().startswith("bearer "):
            return auth_header[7:].strip()
    except Exception:
        pass
    return None


def _extract_account_id_from_jwt(event: dict) -> str | None:
    try:
        claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
        return claims.get("account_id") or claims.get("custom:account_id")
    except (KeyError, TypeError):
        return None


def _extract_actor_id_from_jwt(event: dict) -> str | None:
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
    logger.info("Received event keys: %s", list(event.keys()))

    # ── Parse body ────────────────────────────────────────────────────────────
    if "body" in event:
        try:
            body = json.loads(event["body"]) if isinstance(event["body"], str) else event["body"]
        except (json.JSONDecodeError, TypeError):
            return _api_response(400, {"error": "Invalid JSON in request body"})
    else:
        body = event

    prompt = body.get("prompt", "").strip()
    if not prompt:
        return _api_response(400, {"error": "Missing required field: prompt"})

    if not AGENT_RUNTIME_ARN:
        return _api_response(500, {"error": "AGENT_RUNTIME_ARN environment variable not set"})

    # ── Extract identity from JWT claims ──────────────────────────────────────
    account_id = _extract_account_id_from_jwt(event) or body.get("account_id")
    actor_id   = _extract_actor_id_from_jwt(event) or body.get("actor_id") or account_id
    session_id = body.get("session_id") or str(uuid.uuid4())

    # ── Extract Bearer token to forward to Supervisor ─────────────────────────
    bearer_token = _extract_bearer_token(event)
    logger.info("account_id=%s actor_id=%s session_id=%s has_token=%s",
                account_id, actor_id, session_id, bool(bearer_token))

    # ── Invoke Supervisor AgentCore Runtime ───────────────────────────────────
    try:
        client = _get_client()

        agentcore_payload = json.dumps({
            "prompt": prompt,
            **({"account_id": account_id} if account_id else {}),
            **({"actor_id": actor_id} if actor_id else {}),
            **({"session_id": session_id} if session_id else {}),
            **({"approval_id": body["approval_id"]} if body.get("approval_id") else {}),
        })

        # Forward the user's Bearer token so the Supervisor's Cognito JWT auth passes
        def _inject_bearer(request, **kwargs):
            if bearer_token:
                request.headers["Authorization"] = f"Bearer {bearer_token}"

        client.meta.events.register(
            "before-send.bedrock-agentcore.InvokeAgentRuntime", _inject_bearer
        )

        logger.info("Invoking Supervisor: %s", AGENT_RUNTIME_ARN)
        response = client.invoke_agent_runtime(
            agentRuntimeArn=AGENT_RUNTIME_ARN,
            runtimeSessionId=session_id,
            payload=agentcore_payload,
        )

        client.meta.events.unregister(
            "before-send.bedrock-agentcore.InvokeAgentRuntime", _inject_bearer
        )

        raw_response = response["response"].read()
        agent_result = json.loads(raw_response)
        logger.info("Supervisor responded")

        # Normalise response envelope
        answer = (
            agent_result.get("result")
            or agent_result.get("response")
            or str(agent_result)
        )

        return _api_response(200, {
            "response":        answer,
            "session_id":      session_id,
            "agent_runtime_arn": AGENT_RUNTIME_ARN,
            "mode":            "aws_bedrock_agentcore",
            "routed_to":       agent_result.get("routed_to", "unknown"),
            **({"hitl": agent_result["hitl"]} if agent_result.get("hitl") else {}),
        })

    except Exception as e:
        logger.exception("Error invoking Supervisor")
        return _api_response(500, {"error": str(e)})


def _api_response(status_code: int, body: dict) -> dict:
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
