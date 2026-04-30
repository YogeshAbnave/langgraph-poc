"""
Step 2: Add lambda:InvokeFunction permission to the Worker Agent's IAM role.

The Worker Agent runtime needs to call get-account-balance and
get-account-details Lambda functions. The existing role only has
ECR/logs/Bedrock/Memory permissions — Lambda invoke is missing.

Run: python supervisor/scripts/02_add_lambda_invoke_to_worker_role.py
"""

import boto3
import json
import sys

REGION = "us-east-2"
ACCOUNT = "573054851765"
WORKER_ROLE_NAME = "AmazonBedrockAgentCoreSDKRuntime-us-east-2-0e3ca01f9d"
POLICY_NAME = "BankingLambdaToolsInvoke"

iam = boto3.client("iam")

policy_document = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "InvokeBankingToolLambdas",
            "Effect": "Allow",
            "Action": ["lambda:InvokeFunction"],
            "Resource": [
                f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:get-account-balance",
                f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:get-account-details",
            ],
        }
    ],
}

print(f"Adding Lambda invoke policy to role: {WORKER_ROLE_NAME}")
print(f"  Policy name: {POLICY_NAME}")
print(f"  Lambda functions:")
print(f"    - get-account-balance")
print(f"    - get-account-details")

try:
    iam.put_role_policy(
        RoleName=WORKER_ROLE_NAME,
        PolicyName=POLICY_NAME,
        PolicyDocument=json.dumps(policy_document),
    )
    print("\nDone. Lambda invoke permissions added to Worker Agent role.")
except Exception as e:
    print(f"\nERROR: {e}")
    sys.exit(1)

# Verify
try:
    doc = iam.get_role_policy(RoleName=WORKER_ROLE_NAME, PolicyName=POLICY_NAME)
    print(f"Verified policy attached: {POLICY_NAME}")
except Exception as e:
    print(f"Warning: could not verify policy: {e}")
