"""
setup_memory.py — One-time script to create the AgentCore Memory resource
=========================================================================
Run this ONCE before deploying the agent to provision a memory resource
with both Short-Term Memory (STM) and Long-Term Memory (LTM) strategies.

STM  — stores raw conversation events per session (auto-managed by AgentCore)
LTM  — two extraction strategies that run asynchronously after each event:
         • UserPreference  → learns user preferences across sessions
         • Semantic (Facts) → extracts banking facts / account context

Usage:
    python scripts/setup_memory.py

The script prints the memory ID and ARN at the end.  Copy the memory ID
into .bedrock_agentcore.yaml  →  memory.memory_id  and set the env var
AGENTCORE_MEMORY_ID in your runtime configuration.
"""

import os
import boto3
from bedrock_agentcore.memory import MemoryClient

AWS_REGION = os.environ.get("AWS_REGION", "us-east-2")
MEMORY_NAME = "banking_assistant_memory"
EVENT_EXPIRY_DAYS = 30  # how long raw conversation events are retained

print(f"Creating AgentCore Memory resource in region {AWS_REGION}...")
print(f"  Name            : {MEMORY_NAME}")
print(f"  Event expiry    : {EVENT_EXPIRY_DAYS} days")
print(f"  LTM strategies  : UserPreference + Semantic (Facts)")
print()

client = MemoryClient(region_name=AWS_REGION)

# ---------------------------------------------------------------------------
# LTM strategies
# ---------------------------------------------------------------------------
# 1. UserPreferenceMemoryStrategy
#    Automatically extracts statements like "I prefer X" or "I always want Y"
#    and stores them under /banking/preferences/{actorId}/
#
# 2. SemanticMemoryStrategy
#    Extracts factual knowledge (account numbers, balances mentioned, rules)
#    and stores them under /banking/facts/{actorId}/
# ---------------------------------------------------------------------------
strategies = [
    {
        "userPreferenceMemoryStrategy": {
            "name": "BankingUserPreferences",
            "description": "Learns and retains user preferences across banking sessions",
            "namespaces": ["/banking/preferences/{actorId}/"],
        }
    },
    {
        "semanticMemoryStrategy": {
            "name": "BankingFacts",
            "description": "Extracts and retains banking facts and account context",
            "namespaces": ["/banking/facts/{actorId}/"],
        }
    },
]

try:
    memory = client.create_memory_and_wait(
        name=MEMORY_NAME,
        strategies=strategies,
        description="Banking assistant memory with STM conversation history and LTM user preferences + facts",
        event_expiry_days=EVENT_EXPIRY_DAYS,
    )

    memory_id  = memory.get("memoryId") or memory.get("id")
    memory_arn = memory.get("memoryArn") or memory.get("arn", "")

    print("=" * 60)
    print("✅  Memory resource created successfully!")
    print(f"    Memory ID  : {memory_id}")
    print(f"    Memory ARN : {memory_arn}")
    print(f"    Status     : {memory.get('status')}")
    print()
    print("Next steps:")
    print(f"  1. Set env var  AGENTCORE_MEMORY_ID={memory_id}")
    print(f"  2. Update .bedrock_agentcore.yaml:")
    print(f"       memory:")
    print(f"         mode: STM_AND_LTM")
    print(f"         memory_id: {memory_id}")
    print(f"         memory_arn: {memory_arn}")
    print(f"         memory_name: {MEMORY_NAME}")
    print("  3. Redeploy: agentcore launch -e agentcore_langgraph_runtime.py")
    print("=" * 60)

except Exception as e:
    print(f"❌  Failed to create memory: {e}")
    raise
