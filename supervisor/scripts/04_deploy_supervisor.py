"""
Step 4: Deploy the Supervisor Agent to AgentCore Runtime.

This script:
  1. Runs agentcore configure to set up .bedrock_agentcore.yaml
  2. Runs agentcore launch to build the container and deploy
  3. Captures the Supervisor ARN
  4. Applies Cognito JWT inbound auth (user_client_id)
  5. Attaches IAM permissions for M2M token retrieval + Worker invocation
  6. Saves the Supervisor ARN to outputs.json

Run: python supervisor/scripts/04_deploy_supervisor.py
"""

import boto3
import json
import os
import subprocess
import sys
import time

# Ensure credentials are set for all subprocesses (agentcore CLI)
os.environ.setdefault("AWS_ACCESS_KEY_ID", "YOUR_AWS_ACCESS_KEY_ID")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "YOUR_AWS_SECRET_ACCESS_KEY")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-2")
os.environ["AWS_PROFILE"] = ""  # clear any conflicting profile

REGION = "us-east-2"
ACCOUNT = "573054851765"
SUPERVISOR_DIR = os.path.join(os.path.dirname(__file__), "..")
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "../../Gateway/cognito_config.json")

with open(CONFIG_PATH) as f:
    cognito = json.load(f)

WORKER_RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:us-east-2:573054851765:runtime/"
    "agentcore_langgraph_runtime-P52MOf7Fmi"
)

print("=" * 60)
print("Deploying Supervisor Agent to AgentCore Runtime")
print("=" * 60)

# ── Step 4a: agentcore configure ─────────────────────────────────────────────
print("\n[1/4] Running agentcore configure...")
result = subprocess.run(
    [
        "agentcore", "configure",
        "--entrypoint", "app/SupervisorAgent/main.py",
        "--non-interactive",
    ],
    cwd=SUPERVISOR_DIR,
    capture_output=True,
    text=True,
)
print(result.stdout)
if result.returncode != 0:
    print("STDERR:", result.stderr)
    print("WARNING: configure had non-zero exit, continuing...")

# ── Step 4b: agentcore launch ─────────────────────────────────────────────────
print("\n[2/4] Running agentcore launch (this takes 5-10 minutes)...")
print("      Building Docker image → ECR → AgentCore Runtime...")

launch_result = subprocess.run(
    ["agentcore", "launch"],
    cwd=SUPERVISOR_DIR,
    capture_output=False,  # show live output
    text=True,
)
if launch_result.returncode != 0:
    print("ERROR: agentcore launch failed")
    sys.exit(1)

print("\n[3/4] Fetching Supervisor runtime ARN...")
time.sleep(5)

# Read the ARN from .bedrock_agentcore.yaml (updated by agentcore launch)
import yaml
yaml_path = os.path.join(SUPERVISOR_DIR, ".bedrock_agentcore.yaml")
with open(yaml_path) as f:
    yaml_config = yaml.safe_load(f)

default_agent = yaml_config.get("default_agent", "supervisor_agent")
agent_cfg = yaml_config.get("agents", {}).get(default_agent, {})
supervisor_arn = agent_cfg.get("bedrock_agentcore", {}).get("agent_arn", "")
supervisor_id = agent_cfg.get("bedrock_agentcore", {}).get("agent_id", "")
supervisor_role = agent_cfg.get("aws", {}).get("execution_role", "")

if not supervisor_arn:
    print("ERROR: Could not find Supervisor ARN in .bedrock_agentcore.yaml")
    print("Run 'agentcore status' in the supervisor/ directory to get the ARN")
    sys.exit(1)

print(f"  Supervisor ARN: {supervisor_arn}")
print(f"  Supervisor ID:  {supervisor_id}")
print(f"  Supervisor role: {supervisor_role}")

# ── Step 4c: Apply Cognito JWT inbound auth + WORKER_AGENT_ARN env var ────────
print("\n[4/4] Applying inbound auth and environment variables...")

ctrl = boto3.client("bedrock-agentcore-control", region_name=REGION)
current = ctrl.get_agent_runtime(agentRuntimeId=supervisor_id)

ctrl.update_agent_runtime(
    agentRuntimeId=supervisor_id,
    agentRuntimeArtifact=current["agentRuntimeArtifact"],
    roleArn=current["roleArn"],
    networkConfiguration=current["networkConfiguration"],
    authorizerConfiguration={
        "customJWTAuthorizer": {
            "discoveryUrl": cognito["discovery_url"],
            # Only user_client_id — end users call Supervisor
            "allowedClients": [cognito["user_client_id"]],
        }
    },
    environmentVariables={
        "WORKER_AGENT_ARN": WORKER_RUNTIME_ARN,
        "AWS_REGION": REGION,
    },
)
print("  Inbound auth applied (user_client_id only)")
print(f"  WORKER_AGENT_ARN set to: {WORKER_RUNTIME_ARN}")

# ── Step 4d: Attach IAM permissions to Supervisor role ───────────────────────
if supervisor_role:
    role_name = supervisor_role.split("/")[-1]
    iam = boto3.client("iam")
    print(f"\n  Attaching IAM permissions to Supervisor role: {role_name}")

    iam.put_role_policy(
        RoleName=role_name,
        PolicyName="SupervisorAgentPermissions",
        PolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "AgentCoreIdentityTokens",
                    "Effect": "Allow",
                    "Action": [
                        "bedrock-agentcore:GetResourceOauth2Token",
                        "bedrock-agentcore:GetResourceApiKey",
                    ],
                    "Resource": "*",
                },
                {
                    "Sid": "SecretsManagerForOAuth",
                    "Effect": "Allow",
                    "Action": ["secretsmanager:GetSecretValue"],
                    "Resource": (
                        f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:"
                        "secret:bedrock-agentcore*"
                    ),
                },
                {
                    "Sid": "InvokeWorkerRuntime",
                    "Effect": "Allow",
                    "Action": ["bedrock-agentcore:InvokeAgentRuntime"],
                    "Resource": WORKER_RUNTIME_ARN,
                },
                {
                    "Sid": "BedrockModelInvocation",
                    "Effect": "Allow",
                    "Action": [
                        "bedrock:InvokeModel",
                        "bedrock:InvokeModelWithResponseStream",
                    ],
                    "Resource": "*",
                },
            ],
        }),
    )
    print("  IAM permissions attached.")

# ── Save outputs ──────────────────────────────────────────────────────────────
outputs = {
    "supervisor_arn": supervisor_arn,
    "supervisor_id": supervisor_id,
    "supervisor_role": supervisor_role,
    "worker_arn": WORKER_RUNTIME_ARN,
    "region": REGION,
    "account": ACCOUNT,
    "cognito_user_client_id": cognito["user_client_id"],
    "cognito_agent_client_id": cognito["agent_client_id"],
    "cognito_discovery_url": cognito["discovery_url"],
}

outputs_path = os.path.join(SUPERVISOR_DIR, "outputs.json")
with open(outputs_path, "w") as f:
    json.dump(outputs, f, indent=2)

print(f"\nOutputs saved to: {outputs_path}")

print("\n" + "=" * 60)
print("SUPERVISOR DEPLOYMENT COMPLETE")
print("=" * 60)
print(f"  Supervisor ARN : {supervisor_arn}")
print(f"  Worker ARN     : {WORKER_RUNTIME_ARN}")
print(f"  Inbound auth   : Cognito user_client_id = {cognito['user_client_id']}")
print(f"  Outbound auth  : WorkerAgent-oauth (M2M)")
print("\nNext step: Run 05_update_api_gateway_invoker.py")
