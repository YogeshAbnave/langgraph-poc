"""
Test script: Invokes the AgentCore Runtime agent (backed by a JWT-protected Gateway).

The agent authenticates to the Gateway automatically using the managed credential
created by the CLI (--agent-client-id / --agent-client-secret). From the caller's
perspective, only a standard Cognito JWT is required.

Usage:
    python invoke.py [prompt]
"""

import warnings

import boto3
import json
import os
import sys

warnings.filterwarnings("ignore", category=Warning, module="requests")
warnings.filterwarnings("ignore", message="urllib3")


def get_bearer_token(config: dict) -> str:
    """Get a fresh Cognito access token for the test user."""
    cognito = boto3.client("cognito-idp", region_name=config["region"])
    auth = cognito.initiate_auth(
        ClientId=config["user_client_id"],
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={
            "USERNAME": config["username"],
            "PASSWORD": config["password"],
        },
    )
    return auth["AuthenticationResult"]["AccessToken"]


def get_agent_arn() -> str:
    """Read the deployed agent ARN from .bedrock_agentcore.yaml (new CLI format)."""
    import yaml
    base = os.path.dirname(os.path.abspath(__file__))
    config_file = os.path.join(base, ".bedrock_agentcore.yaml")
    if not os.path.exists(config_file):
        raise FileNotFoundError(
            "No .bedrock_agentcore.yaml found. Run 'agentcore configure' and 'agentcore launch' first."
        )
    with open(config_file) as f:
        config = yaml.safe_load(f)
    default_agent = config.get("default_agent")
    agents = config.get("agents", {})
    agent_cfg = agents.get(default_agent, {})
    arn = agent_cfg.get("bedrock_agentcore", {}).get("agent_arn")
    if arn:
        return arn
    raise ValueError("No deployed agent ARN found. Run 'agentcore launch' first.")


def parse_event_stream(response: dict) -> str:
    parts = []
    for event in response.get("response", []):
        raw = event if isinstance(event, bytes) else event.get("chunk", {}).get("bytes", b"")
        if raw:
            try:
                decoded = json.loads(raw.decode("utf-8"))
                if isinstance(decoded, str):
                    parts.append(decoded)
                elif isinstance(decoded, dict):
                    content = decoded.get("content", [])
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            parts.append(c["text"])
                        elif isinstance(c, str):
                            parts.append(c)
                    if not content and "message" in decoded:
                        msg = decoded["message"]
                        if isinstance(msg, dict):
                            for c in msg.get("content", []):
                                if isinstance(c, dict) and c.get("type") == "text":
                                    parts.append(c["text"])
            except Exception:
                parts.append(raw.decode("utf-8"))
    return "\n".join(parts) if parts else "(no response)"


def main():
    prompt = sys.argv[1] if len(sys.argv) > 1 else "What tools do you have available?"

    try:
        with open("cognito_config.json") as f:
            config = json.load(f)
    except FileNotFoundError:
        print("ERROR: cognito_config.json not found. Run 'python setup_cognito.py' first.")
        sys.exit(1)

    region = config["region"]
    client = boto3.client("bedrock-agentcore", region_name=region)

    print("Resolving deployed agent ARN...")
    agent_arn = get_agent_arn()
    print(f"  Agent ARN: {agent_arn}")

    # --- Test 1: No bearer token ---
    print("\n[Test 1] Invoking WITHOUT bearer token (expect AccessDeniedException)...")
    try:
        resp = client.invoke_agent_runtime(
            agentRuntimeArn=agent_arn,
            runtimeUserId="testuser",
            qualifier="DEFAULT",
            payload=json.dumps({"prompt": prompt}),
        )
        print("  Unexpected success:", resp)
    except client.exceptions.AccessDeniedException as exc:
        print(f"  Correctly rejected: {exc}")
    except Exception as exc:
        print(f"  Error: {type(exc).__name__}: {exc}")

    # --- Test 2: Valid user bearer token ---
    print("\n[Test 2] Invoking WITH Cognito bearer token (expect success)...")
    bearer_token = get_bearer_token(config)
    print(f"  Token obtained (first 20 chars): {bearer_token[:20]}...")
    print(f"  Prompt: '{prompt}'")

    try:
        def _inject_bearer(request, **kwargs):
            request.headers["Authorization"] = f"Bearer {bearer_token}"

        client.meta.events.register(
            "before-send.bedrock-agentcore.InvokeAgentRuntime", _inject_bearer
        )
        resp = client.invoke_agent_runtime(
            agentRuntimeArn=agent_arn,
            runtimeUserId=config["username"],
            qualifier="DEFAULT",
            payload=json.dumps({"prompt": prompt}),
        )
        client.meta.events.unregister(
            "before-send.bedrock-agentcore.InvokeAgentRuntime", _inject_bearer
        )
        result = parse_event_stream(resp)
        print(f"\nAgent response:\n{result}")
    except Exception as exc:
        print(f"  Error: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
