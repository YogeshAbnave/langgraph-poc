"""
HITL Operator Tool: Reject a pending request.

Usage:
    python supervisor/scripts/hitl_reject.py <approval_id>
    python supervisor/scripts/hitl_reject.py <approval_id> "Insufficient funds"
"""

import boto3
import sys
from datetime import datetime

REGION = "us-east-2"
TABLE_NAME = "banking-hitl-approvals"


def reject(approval_id: str, reason: str = "Rejected by operator") -> None:
    dynamodb = boto3.resource("dynamodb", region_name=REGION)
    table = dynamodb.Table(TABLE_NAME)

    resp = table.get_item(Key={"approval_id": approval_id})
    item = resp.get("Item")
    if not item:
        print(f"ERROR: Approval ID '{approval_id}' not found.")
        sys.exit(1)

    if item["status"] != "PENDING":
        print(f"WARNING: Request is already '{item['status']}' — not PENDING.")
        return

    table.update_item(
        Key={"approval_id": approval_id},
        UpdateExpression="SET #s = :s, rejected_at = :t, rejection_reason = :r",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": "REJECTED",
            ":t": datetime.utcnow().isoformat(),
            ":r": reason,
        },
    )

    print(f"❌ REJECTED: {approval_id}")
    print(f"   Operation: {item.get('operation_type', 'N/A')}")
    print(f"   Actor:     {item.get('actor_id', 'N/A')}")
    print(f"   Reason:    {reason}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python hitl_reject.py <approval_id> [reason]")
        sys.exit(1)
    approval_id = sys.argv[1]
    reason = sys.argv[2] if len(sys.argv) > 2 else "Rejected by operator"
    reject(approval_id, reason)
