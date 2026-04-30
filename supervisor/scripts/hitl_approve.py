"""
HITL Operator Tool: Approve a pending request.

Usage:
    python supervisor/scripts/hitl_approve.py <approval_id>
    python supervisor/scripts/hitl_approve.py <approval_id> "Approved by manager John"
"""

import boto3
import sys
from datetime import datetime

REGION = "us-east-2"
TABLE_NAME = "banking-hitl-approvals"


def approve(approval_id: str, note: str = "Approved by operator") -> None:
    dynamodb = boto3.resource("dynamodb", region_name=REGION)
    table = dynamodb.Table(TABLE_NAME)

    # Get current item
    resp = table.get_item(Key={"approval_id": approval_id})
    item = resp.get("Item")
    if not item:
        print(f"ERROR: Approval ID '{approval_id}' not found.")
        sys.exit(1)

    if item["status"] != "PENDING":
        print(f"WARNING: Request is already '{item['status']}' — not PENDING.")
        print(f"  Current status: {item['status']}")
        return

    # Update status to APPROVED
    table.update_item(
        Key={"approval_id": approval_id},
        UpdateExpression="SET #s = :s, approved_at = :t, approval_note = :n",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": "APPROVED",
            ":t": datetime.utcnow().isoformat(),
            ":n": note,
        },
    )

    print(f"✅ APPROVED: {approval_id}")
    print(f"   Operation: {item.get('operation_type', 'N/A')}")
    print(f"   Actor:     {item.get('actor_id', 'N/A')}")
    print(f"   Prompt:    {item.get('prompt', 'N/A')[:100]}")
    print(f"   Note:      {note}")
    print(f"\nThe user can now resubmit their request with:")
    print(f'  {{"prompt": "{item.get("prompt","")}", "approval_id": "{approval_id}"}}')


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python hitl_approve.py <approval_id> [note]")
        sys.exit(1)
    approval_id = sys.argv[1]
    note = sys.argv[2] if len(sys.argv) > 2 else "Approved by operator"
    approve(approval_id, note)
