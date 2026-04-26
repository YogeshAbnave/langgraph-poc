"""
test_local.py — Local test suite for the Banking Assistant
===========================================================
Runs without AWS credentials by:
  1. Testing Lambda handlers directly (pure Python, no AWS)
  2. Testing @tool functions with USE_LAMBDA=false (local dispatch)
  3. Testing the full LangGraph graph with a mocked LLM

Run:
    python test_local.py
"""

import json
import os
import sys

# Ensure project root is on the path (works from tests/ subdirectory too)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Force local mode — no real Lambda or Bedrock calls
os.environ["USE_LAMBDA"] = "false"

PASS = "\033[92m PASS\033[0m"
FAIL = "\033[91m FAIL\033[0m"
HEAD = "\033[94m{}\033[0m"

results = []

def check(name, condition, detail=""):
    status = PASS if condition else FAIL
    print(f"  [{status}] {name}" + (f"\n         {detail}" if detail else ""))
    results.append(condition)

# ─────────────────────────────────────────────────────────────────────────────
print(HEAD.format("\n=== 1. Lambda Handler: get_account_balance ==="))
# ─────────────────────────────────────────────────────────────────────────────
from lambda_handlers.get_account_balance import handler as balance_handler

# 1a — known account, below minimum (ACC001 has $1200.50, min $2500)
evt = {"parameters": [{"name": "account_id", "value": "ACC001"}]}
r = balance_handler(evt)
body = json.loads(r["response"]["responseBody"]["application/json"]["body"])
print(f"  ACC001 balance response: {json.dumps(body, indent=4)}")
check("ACC001 balance returned",       body.get("current_balance") == 1200.50)
check("ACC001 minimum_balance correct", body.get("minimum_balance_required") == 2500.00)
check("ACC001 is_compliant = False",   body.get("is_compliant") == False)
check("ACC001 penalty_exposure = 25",  body.get("penalty_exposure") == 25.00)

# 1b — known account, above minimum (ACC002 has $820, min $500)
evt2 = {"parameters": [{"name": "account_id", "value": "ACC002"}]}
r2 = balance_handler(evt2)
body2 = json.loads(r2["response"]["responseBody"]["application/json"]["body"])
print(f"\n  ACC002 balance response: {json.dumps(body2, indent=4)}")
check("ACC002 is_compliant = True",    body2.get("is_compliant") == True)
check("ACC002 penalty_exposure = 0",   body2.get("penalty_exposure") == 0.0)

# 1c — unknown account
evt3 = {"parameters": [{"name": "account_id", "value": "ACC999"}]}
r3 = balance_handler(evt3)
body3 = json.loads(r3["response"]["responseBody"]["application/json"]["body"])
check("Unknown account returns error", "error" in body3)

# 1d — missing parameter
r4 = balance_handler({"parameters": []})
body4 = json.loads(r4["response"]["responseBody"]["application/json"]["body"])
check("Missing param returns error",   "error" in body4)

# ─────────────────────────────────────────────────────────────────────────────
print(HEAD.format("\n=== 2. Lambda Handler: get_account_details ==="))
# ─────────────────────────────────────────────────────────────────────────────
from lambda_handlers.get_account_details import handler as details_handler

evt = {"parameters": [{"name": "account_id", "value": "ACC001"}]}
r = details_handler(evt)
body = json.loads(r["response"]["responseBody"]["application/json"]["body"])
print(f"  ACC001 details response: {json.dumps(body, indent=4)}")
check("ACC001 owner correct",   body.get("owner") == "Alice Johnson")
check("ACC001 email present",   "@" in body.get("email", ""))
check("ACC001 member_since",    body.get("member_since") == "2018-03-15")
check("ACC001 account_type",    body.get("account_type") == "Premium Checking")

# ─────────────────────────────────────────────────────────────────────────────
print(HEAD.format("\n=== 3. @tool functions (local dispatch, no Lambda) ==="))
# ─────────────────────────────────────────────────────────────────────────────
from tools.banking_tools import get_account_balance, get_account_details

result = get_account_balance.invoke({"account_id": "ACC003"})
print(f"  get_account_balance(ACC003): {result}")
check("Tool: balance returned for ACC003",  result.get("current_balance") == 1100.50)
check("Tool: account_type Savings",         result.get("account_type") == "Savings")

result2 = get_account_details.invoke({"account_id": "ACC002"})
print(f"  get_account_details(ACC002): {result2}")
check("Tool: owner Bob Smith",   result2.get("owner") == "Bob Smith")
check("Tool: status active",     result2.get("status") == "active")

# ─────────────────────────────────────────────────────────────────────────────
print(HEAD.format("\n=== 4. Full LangGraph graph (mocked LLM) ==="))
# ─────────────────────────────────────────────────────────────────────────────
import operator
from typing import Annotated, Sequence
from typing_extensions import TypedDict
from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode

class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], operator.add]

tools = [get_account_balance, get_account_details]

# ── Build the graph (same logic as agentcore_langgraph_runtime.py) ──────────
def should_continue(state: AgentState) -> str:
    last = state["messages"][-1]
    if not getattr(last, "tool_calls", None):
        return "end"
    return "continue"

# We'll inject the mock llm via a closure
_mock_llm = None

def call_model(state: AgentState) -> dict:
    response = _mock_llm.invoke(state["messages"])
    return {"messages": [response]}

graph_builder = StateGraph(AgentState)
graph_builder.add_node("call_model", call_model)
graph_builder.add_node("tools", ToolNode(tools=tools))
graph_builder.add_edge(START, "call_model")
graph_builder.add_conditional_edges("call_model", should_continue,
                                    {"continue": "tools", "end": END})
graph_builder.add_edge("tools", "call_model")
graph = graph_builder.compile()

# ── Test 4a: LLM calls get_account_balance then gives final answer ───────────
print("\n  Test 4a: tool call -> tool result -> final answer")

# Turn 1: LLM emits a tool call for get_account_balance
tool_call_msg = AIMessage(
    content="",
    tool_calls=[{
        "id":   "call_001",
        "name": "get_account_balance",
        "args": {"account_id": "ACC001"},
        "type": "tool_call",
    }],
)
# Turn 2: LLM sees tool result and gives final answer
final_msg = AIMessage(
    content=(
        "The balance for account ACC001 is $1,200.50. "
        "The account is below the $2,500 minimum for Premium Checking "
        "and is subject to a $25.00 monthly fee."
    )
)

mock_llm = MagicMock()
mock_llm.invoke.side_effect = [tool_call_msg, final_msg]
_mock_llm = mock_llm

initial_state: AgentState = {
    "messages": [HumanMessage(content="What is the balance for ACC001?")]
}
output = graph.invoke(initial_state)

msgs = output["messages"]
print(f"  Total messages in history: {len(msgs)}")
for i, m in enumerate(msgs):
    print(f"    [{i}] {type(m).__name__}: {str(m.content)[:80]}")

check("Graph: LLM called twice (tool call + synthesis)", mock_llm.invoke.call_count == 2)
check("Graph: ToolMessage present in history",
      any(isinstance(m, ToolMessage) for m in msgs))
check("Graph: Final answer is AIMessage",  isinstance(msgs[-1], AIMessage))
check("Graph: Final answer contains balance",
      "1,200.50" in msgs[-1].content or "1200.50" in msgs[-1].content)

# ── Test 4b: LLM answers directly (no tool call needed) ─────────────────────
print("\n  Test 4b: direct answer (no tool call)")

direct_msg = AIMessage(content="I can help you with account balance queries.")
mock_llm2 = MagicMock()
mock_llm2.invoke.return_value = direct_msg
_mock_llm = mock_llm2

output2 = graph.invoke({
    "messages": [HumanMessage(content="Hello, what can you do?")]
})
check("Graph: LLM called once for direct answer", mock_llm2.invoke.call_count == 1)
check("Graph: No ToolMessage for direct answer",
      not any(isinstance(m, ToolMessage) for m in output2["messages"]))
check("Graph: Final content correct",
      "account balance" in output2["messages"][-1].content)

# ─────────────────────────────────────────────────────────────────────────────
print(HEAD.format("\n=== 5. Knowledge Base data integrity ==="))
# ─────────────────────────────────────────────────────────────────────────────
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

with open(os.path.join(_ROOT, "data", "account_data.json")) as f:
    acct_data = json.load(f)
with open(os.path.join(_ROOT, "data", "penalty_rules.json")) as f:
    penalty_data = json.load(f)

check("account_data.json has 3 accounts",  len(acct_data["accounts"]) == 3)
check("All accounts have required fields",
      all({"owner","email","current_balance","minimum_balance","account_type"} <= set(v.keys())
          for v in acct_data["accounts"].values()))
check("penalty_rules.json has 3 account types",
      len(penalty_data["penalty_rules"]) == 3)
check("All penalty rules have minimum_balance",
      all("minimum_balance" in v for v in penalty_data["penalty_rules"].values()))

# ─────────────────────────────────────────────────────────────────────────────
print(HEAD.format("\n=== Summary ==="))
# ─────────────────────────────────────────────────────────────────────────────
passed = sum(results)
total  = len(results)
color  = "\033[92m" if passed == total else "\033[91m"
print(f"  {color}{passed}/{total} tests passed\033[0m\n")
sys.exit(0 if passed == total else 1)
