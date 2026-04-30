"""
Step 5: Update the api-gateway-invoker Lambda to point to the Supervisor Agent.

The existing api-gateway-invoker currently points directly to the Worker Agent.
We update its AGENT_RUNTIME_ARN env var to point to the Supervisor instead,
so the full flow becomes:

  Client → API Gateway → api-gateway-invoker → Supervisor → Worker → Lambda tools

Run: python supervisor/scripts/05_update_api_gateway_invoker.py
"""

import boto3
import json
import os
import sys

REGION = "us-east-2"
INVOKER_FN = "api-gateway-invoker"

# Load Supervisor ARN from outputs.json (written by 04_deploy_supervisor.py)
OUTPUTS_PATH = os.path.join(os.path.dirname(__file__), "../outputs.json")

if not os.path.exists(OUTPUTS_PATH):
    print(f"ERROR: {OUTPUTS_PATH} not found.")
    print("Run 04_deploy_supervisor.py first to deploy the Supervisor Agent.")
    sys.exit(1)

with open(OUTPUTS_PATH) as f:
    outputs = json.load(f)

supervisor_arn = outputs["supervisor_arn"]
print(f"Updating {INVOKER_FN} to point to Supervisor Agent")
print(f"  Supervisor ARN: {supervisor_arn}")

lam = boto3.client("lambda", region_name=REGION)

# Get current config
current = lam.get_function_configuration(FunctionName=INVOKER_FN)
current_env = current.get("Environment", {}).get("Variables", {})
old_arn = current_env.get("AGENT_RUNTIME_ARN", "")
print(f"  Old AGENT_RUNTIME_ARN: {old_arn}")

# Update env var to point to Supervisor
new_env = {
    **current_env,
    "AGENT_RUNTIME_ARN": supervisor_arn,
    "BEDROCK_REGION": REGION,
}

try:
    lam.update_function_configuration(
        FunctionName=INVOKER_FN,
        Environment={"Variables": new_env},
    )
    print(f"\nDone. {INVOKER_FN} now routes to Supervisor Agent.")
    print(f"  New AGENT_RUNTIME_ARN: {supervisor_arn}")
except Exception as e:
    print(f"\nERROR: {e}")
    sys.exit(1)

# Also update the invoker Lambda's IAM role to allow invoking the Supervisor
print("\nUpdating invoker Lambda IAM role to allow Supervisor invocation...")
iam = boto3.client("iam")
invoker_role = current.get("Role", "").split("/")[-1]

if invoker_role:
    try:
        iam.put_role_policy(
            RoleName=invoker_role,
            PolicyName="InvokeSupervisorRuntime",
            PolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["bedrock-agentcore:InvokeAgentRuntime"],
                        "Resource": supervisor_arn,
                    }
                ],
            }),
        )
        print(f"  IAM policy updated for role: {invoker_role}")
    except Exception as e:
        print(f"  Warning: could not update IAM policy: {e}")

print("\nFull request flow is now:")
print("  Client")
print("    → API Gateway (JWT auth)")
print("    → api-gateway-invoker Lambda")
print(f"    → Supervisor Agent ({supervisor_arn[-30:]}...)")
print("    → Worker Agent (LangGraph, M2M auth)")
print("    → Lambda tools (get-account-balance, get-account-details)")
