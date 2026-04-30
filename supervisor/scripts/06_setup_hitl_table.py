"""
Step 6: Create DynamoDB table for Human-in-the-Loop (HITL) approvals.

The Supervisor Agent writes pending approval requests here.
Human operators read and update the status (APPROVED/REJECTED).

Table schema:
  PK: approval_id (String)
  Attributes: actor_id, session_id, prompt, operation_type,
              status (PENDING/APPROVED/REJECTED), created_at, ttl

Run: python supervisor/scripts/06_setup_hitl_table.py
"""

import boto3
import json
import sys

REGION = "us-east-2"
TABLE_NAME = "banking-hitl-approvals"

dynamodb = boto3.client("dynamodb", region_name=REGION)

print(f"Creating DynamoDB HITL table: {TABLE_NAME}")

# Check if table already exists
try:
    existing = dynamodb.describe_table(TableName=TABLE_NAME)
    print(f"  Table already exists: {existing['Table']['TableStatus']}")
    print(f"  ARN: {existing['Table']['TableArn']}")
    sys.exit(0)
except dynamodb.exceptions.ResourceNotFoundException:
    pass

# Create table
try:
    resp = dynamodb.create_table(
        TableName=TABLE_NAME,
        KeySchema=[
            {"AttributeName": "approval_id", "KeyType": "HASH"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "approval_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
        Tags=[
            {"Key": "Project", "Value": "BankingAgent"},
            {"Key": "Component", "Value": "HITL"},
        ],
    )
    # Enable TTL separately after table creation
    waiter = dynamodb.get_waiter("table_exists")
    waiter.wait(TableName=TABLE_NAME)
    dynamodb.update_time_to_live(
        TableName=TABLE_NAME,
        TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"},
    )
    print(f"  Created table: {resp['TableDescription']['TableArn']}")
    print(f"  Status: {resp['TableDescription']['TableStatus']}")

    # Wait for table to be active
    print("  Waiting for table to become ACTIVE...")
    waiter = dynamodb.get_waiter("table_exists")
    waiter.wait(TableName=TABLE_NAME)
    print("  Table is ACTIVE.")

except Exception as e:
    print(f"  ERROR: {e}")
    sys.exit(1)

# Add IAM permission for Supervisor role to access DynamoDB
print("\nAdding DynamoDB permissions to Supervisor role...")
iam = boto3.client("iam")
SUPERVISOR_ROLE = "AmazonBedrockAgentCoreSDKRuntime-us-east-2-5130eca7b1"
ACCOUNT = "573054851765"

try:
    iam.put_role_policy(
        RoleName=SUPERVISOR_ROLE,
        PolicyName="HITLDynamoDBAccess",
        PolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "HITLTableAccess",
                    "Effect": "Allow",
                    "Action": [
                        "dynamodb:PutItem",
                        "dynamodb:GetItem",
                        "dynamodb:UpdateItem",
                        "dynamodb:Query",
                        "dynamodb:Scan",
                    ],
                    "Resource": f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{TABLE_NAME}",
                }
            ],
        }),
    )
    print(f"  DynamoDB permissions added to role: {SUPERVISOR_ROLE}")
except Exception as e:
    print(f"  Warning: could not add DynamoDB permissions: {e}")

print(f"\nDone. HITL table ready: {TABLE_NAME}")
print("\nTo approve a request:")
print("  python supervisor/scripts/hitl_approve.py <approval_id>")
print("\nTo reject a request:")
print("  python supervisor/scripts/hitl_reject.py <approval_id> 'reason'")
print("\nTo list pending requests:")
print("  python supervisor/scripts/hitl_list.py")
