"""
Step 3: Update the Worker Agent runtime configuration:
  - Set USE_LAMBDA=true so it calls real AWS Lambda functions
  - Lock inbound auth to agent_client_id ONLY (users cannot bypass Supervisor)
  - Keep existing memory env vars

Run: python supervisor/scripts/03_update_worker_runtime.py
"""

import boto3
import json
import sys
import os

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "../../Gateway/cognito_config.json")
with open(CONFIG_PATH) as f:
    config = json.load(f)

REGION = config["region"]
WORKER_RUNTIME_ID = "agentcore_langgraph_runtime-P52MOf7Fmi"

ctrl = boto3.client("bedrock-agentcore-control", region_name=REGION)

print(f"Fetching current Worker runtime config: {WORKER_RUNTIME_ID}")
current = ctrl.get_agent_runtime(agentRuntimeId=WORKER_RUNTIME_ID)

print(f"  Current status: {current.get('status')}")
print(f"  Current env vars: {current.get('environmentVariables', {})}")
print(f"  Current auth: {current.get('authorizerConfiguration', {})}")

# Merge existing env vars with new ones
existing_env = current.get("environmentVariables", {})
new_env = {
    **existing_env,
    "USE_LAMBDA": "true",
    "LAMBDA_BALANCE_FN": "get-account-balance",
    "LAMBDA_DETAILS_FN": "get-account-details",
    "AWS_REGION": REGION,
}

# Inbound auth: ONLY agent_client_id — blocks direct user access
new_auth = {
    "customJWTAuthorizer": {
        "discoveryUrl": config["discovery_url"],
        # Only the agent_client_id is allowed — Supervisor uses M2M tokens
        # issued for this client. Users (user_client_id) are blocked.
        "allowedClients": [config["agent_client_id"]],
    }
}

print(f"\nUpdating Worker runtime:")
print(f"  New env vars: {new_env}")
print(f"  New auth (allowedClients): {[config['agent_client_id']]}")
print(f"  (agent_client_id = {config['agent_client_id']})")

try:
    ctrl.update_agent_runtime(
        agentRuntimeId=WORKER_RUNTIME_ID,
        agentRuntimeArtifact=current["agentRuntimeArtifact"],
        roleArn=current["roleArn"],
        networkConfiguration=current["networkConfiguration"],
        authorizerConfiguration=new_auth,
        environmentVariables=new_env,
    )
    print("\nDone. Worker runtime updated successfully.")
    print("  - USE_LAMBDA=true (calls real AWS Lambda)")
    print(f"  - Inbound auth locked to agent_client_id: {config['agent_client_id']}")
    print("  - Users cannot call Worker directly (must go through Supervisor)")
except Exception as e:
    print(f"\nERROR updating Worker runtime: {e}")
    sys.exit(1)
