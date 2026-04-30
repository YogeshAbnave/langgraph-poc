"""
Step 1: Create the WorkerAgent-oauth credential provider in AgentCore Identity.

This is the M2M credential the Supervisor uses to obtain a Bearer token
to call the Worker Agent runtime. It reuses the existing Cognito
agent_client_id from the Gateway POC.

Run: python supervisor/scripts/01_create_worker_oauth_credential.py
"""

import boto3
import json
import sys
import os

# Load Cognito config from Gateway POC
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "../../Gateway/cognito_config.json")
with open(CONFIG_PATH) as f:
    config = json.load(f)

region = config["region"]
ctrl = boto3.client("bedrock-agentcore-control", region_name=region)

PROVIDER_NAME = "WorkerAgent-oauth"

print(f"Checking for existing credential provider: {PROVIDER_NAME}")
try:
    providers = ctrl.list_oauth2_credential_providers()
    existing = {p["name"] for p in providers.get("credentialProviders", [])}
except Exception as e:
    print(f"Warning: could not list providers: {e}")
    existing = set()

if PROVIDER_NAME in existing:
    print(f"  '{PROVIDER_NAME}' already exists — skipping creation.")
    print(f"  To recreate, delete it first via the AWS console or CLI.")
else:
    print(f"  Creating '{PROVIDER_NAME}'...")
    try:
        result = ctrl.create_oauth2_credential_provider(
            name=PROVIDER_NAME,
            credentialProviderVendor="CustomOauth2",
            oauth2ProviderConfigInput={
                "customOauth2ProviderConfig": {
                    "clientId": config["agent_client_id"],
                    "clientSecret": config["agent_client_secret"],
                    "oauthDiscovery": {
                        "discoveryUrl": config["discovery_url"],
                    },
                }
            },
        )
        print(f"  Created: {result.get('credentialProviderArn', 'OK')}")
    except Exception as e:
        print(f"  ERROR creating credential provider: {e}")
        sys.exit(1)

print("\nDone. WorkerAgent-oauth credential provider is ready.")
print(f"  Cognito pool:        {config['pool_id']}")
print(f"  agent_client_id:     {config['agent_client_id']}")
print(f"  discovery_url:       {config['discovery_url']}")
