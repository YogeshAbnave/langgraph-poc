"""
HITL Operator Tool: List all pending approval requests.

Usage:
    python supervisor/scripts/hitl_list.py
    python supervisor/scripts/hitl_list.py --all     # include approved/rejected
"""

import boto3
import sys
from datetime import datetime

REGION = "us-east-2"
TABLE_NAME = "banking-hitl-approvals"


def list_requests(show_all: bool = False) -> None:
    dynamodb = boto3.resource("dynamodb", region_name=REGION)
    table = dynamodb.Table(TABLE_NAME)

    resp = table.scan()
    items = resp.get("Items", [])

    if not show_all:
        items = [i for i in items if i.get("status") == "PENDING"]

    if not items:
        status_filter = "any" if show_all else "PENDING"
        print(f"No {status_filter} HITL requests found.")
        return

    # Sort by created_at
    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)

    status_icons = {"PENDING": "⏳", "APPROVED": "✅", "REJECTED": "❌"}

    print(f"\n{'='*70}")
    print(f"  HITL Approval Requests ({len(items)} found)")
    print(f"{'='*70}")

    for item in items:
        icon = status_icons.get(item.get("status", ""), "?")
        print(f"\n{icon} [{item.get('status','?')}] {item.get('approval_id','?')}")
        print(f"   Operation : {item.get('operation_type', 'N/A')}")
        print(f"   Actor     : {item.get('actor_id', 'N/A')}")
        print(f"   Created   : {item.get('created_at', 'N/A')}")
        print(f"   Prompt    : {item.get('prompt', 'N/A')[:80]}...")
        if item.get("approval_note"):
            print(f"   Note      : {item['approval_note']}")
        if item.get("rejection_reason"):
            print(f"   Reason    : {item['rejection_reason']}")

    print(f"\n{'='*70}")
    pending = sum(1 for i in items if i.get("status") == "PENDING")
    print(f"  Pending: {pending}  |  Total shown: {len(items)}")
    print(f"{'='*70}\n")

    if pending > 0:
        print("To approve: python supervisor/scripts/hitl_approve.py <approval_id>")
        print("To reject:  python supervisor/scripts/hitl_reject.py <approval_id> 'reason'")


if __name__ == "__main__":
    show_all = "--all" in sys.argv
    list_requests(show_all)
