"""
local_server.py — Local API Gateway simulation for Postman testing
====================================================================
Replicates the full architecture from LangGraph.md locally:

  Postman → FastAPI (this file) → LangGraph graph → tools → response

Modes:
  --mock   Uses a scripted mock LLM (no AWS credentials needed)
  --live   Uses real ChatBedrock / Nova Pro (requires valid AWS creds)

Run:
  python local_server.py          # mock mode (default)
  python local_server.py --live   # live Bedrock mode

Endpoints:
  POST /          {"prompt": "What is the balance for ACC001?"}
  POST /invoke    same as above (alias)
  GET  /health    health check
  GET  /accounts  list available test accounts
"""

import argparse
import json
import operator
import os
import sys
import uuid
from typing import Annotated, Sequence

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from pydantic import BaseModel
from typing_extensions import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

# Force local Lambda dispatch (no real AWS Lambda)
os.environ["USE_LAMBDA"] = "false"

# ── Parse CLI args before FastAPI starts ─────────────────────────────────────
parser = argparse.ArgumentParser(description="Banking Agent local server")
parser.add_argument("--live", action="store_true",
                    help="Use real AWS Bedrock (requires valid credentials)")
parser.add_argument("--port", type=int, default=8000)
parser.add_argument("--host", type=str, default="0.0.0.0")
args, _ = parser.parse_known_args()

USE_LIVE_LLM = args.live

# ── Tools (always local) ─────────────────────────────────────────────────────
from tools.banking_tools import get_account_balance, get_account_details

tools = [get_account_balance, get_account_details]

# ── LLM setup ────────────────────────────────────────────────────────────────
if USE_LIVE_LLM:
    import boto3
    from langchain_aws import ChatBedrock

    AWS_REGION = os.environ.get("AWS_REGION", "us-east-2")
    bedrock_client = boto3.client("bedrock-runtime", region_name=AWS_REGION)
    llm = ChatBedrock(
        model_id="us.amazon.nova-pro-v1:0",
        client=bedrock_client,
        model_kwargs={"temperature": 0.1, "max_tokens": 2000},
    )
    llm_with_tools = llm.bind_tools(tools)
    print("Mode: LIVE  (AWS Bedrock / Nova Pro)")
else:
    # Scripted mock LLM — deterministic responses keyed on account_id
    # so Postman gets realistic, readable answers without any AWS calls.
    class MockLLM:
        """
        Simulates Nova Pro tool-calling behaviour:
          Turn 1 → emits tool_calls based on keywords in the prompt
          Turn 2 → synthesises a natural-language answer from ToolMessages
        """
        def __init__(self):
            self._call_count = 0
            self._pending_tool_calls = []

        def _reset(self):
            self._call_count = 0
            self._pending_tool_calls = []

        def bind_tools(self, _tools):
            return self  # self handles tool dispatch

        def invoke(self, messages: list) -> AIMessage:
            self._call_count += 1

            # ── Turn 1: decide which tools to call ───────────────────────────
            if self._call_count == 1:
                human_text = ""
                for m in messages:
                    if isinstance(m, HumanMessage):
                        human_text = m.content.lower()
                        break

                # Extract account_id from the prompt (ACC001 / ACC002 / ACC003)
                account_id = None
                for candidate in ["acc001", "acc002", "acc003"]:
                    if candidate in human_text:
                        account_id = candidate.upper()
                        break
                if not account_id:
                    account_id = "ACC001"  # default

                wants_balance = any(w in human_text for w in
                                    ["balance", "minimum", "complian", "penalty"])
                wants_details = any(w in human_text for w in
                                    ["detail", "owner", "email", "phone", "address",
                                     "member", "who"])

                # Default: balance query
                if not wants_balance and not wants_details:
                    wants_balance = True

                tool_calls = []
                if wants_balance:
                    tool_calls.append({
                        "id":   f"call_bal_{uuid.uuid4().hex[:6]}",
                        "name": "get_account_balance",
                        "args": {"account_id": account_id},
                        "type": "tool_call",
                    })
                if wants_details:
                    tool_calls.append({
                        "id":   f"call_det_{uuid.uuid4().hex[:6]}",
                        "name": "get_account_details",
                        "args": {"account_id": account_id},
                        "type": "tool_call",
                    })

                self._pending_tool_calls = tool_calls
                return AIMessage(content="", tool_calls=tool_calls)

            # ── Turn 2: synthesise answer from ToolMessages ──────────────────
            tool_results = {}
            for m in messages:
                if isinstance(m, ToolMessage):
                    try:
                        data = json.loads(m.content) if isinstance(m.content, str) else m.content
                        tool_results[m.name] = data
                    except Exception:
                        tool_results[m.name] = m.content

            parts = []

            if "get_account_balance" in tool_results:
                b = tool_results["get_account_balance"]
                acc   = b.get("account_id", "N/A")
                bal   = b.get("current_balance", 0)
                atype = b.get("account_type", "N/A")
                minb  = b.get("minimum_balance_required", 0)
                comp  = b.get("is_compliant", True)
                pen   = b.get("penalty_exposure", 0)

                parts.append(
                    f"The current balance for account {acc} is ${bal:,.2f} USD."
                )
                parts.append(
                    f"This is a {atype} account with a minimum balance requirement of ${minb:,.2f}."
                )
                if comp:
                    parts.append("The account is compliant — no penalty fees apply.")
                else:
                    parts.append(
                        f"The account is currently BELOW the minimum balance. "
                        f"A monthly penalty fee of ${pen:,.2f} will be applied."
                    )

            if "get_account_details" in tool_results:
                d = tool_results["get_account_details"]
                parts.append(
                    f"Account holder: {d.get('owner', 'N/A')} | "
                    f"Email: {d.get('email', 'N/A')} | "
                    f"Member since: {d.get('member_since', 'N/A')}."
                )

            if not parts:
                parts.append("I was unable to retrieve the requested account information.")

            return AIMessage(content=" ".join(parts))

    _mock_instance = MockLLM()
    llm_with_tools = _mock_instance
    print("Mode: MOCK  (no AWS credentials needed)")


# ── AgentState & graph ────────────────────────────────────────────────────────
class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], operator.add]


def call_model(state: AgentState) -> dict:
    response = llm_with_tools.invoke(state["messages"])
    return {"messages": [response]}


def should_continue(state: AgentState) -> str:
    last = state["messages"][-1]
    if not getattr(last, "tool_calls", None):
        return "end"
    return "continue"


graph_builder = StateGraph(AgentState)
graph_builder.add_node("call_model", call_model)
graph_builder.add_node("tools", ToolNode(tools=tools))
graph_builder.add_edge(START, "call_model")
graph_builder.add_conditional_edges(
    "call_model", should_continue, {"continue": "tools", "end": END}
)
graph_builder.add_edge("tools", "call_model")
graph = graph_builder.compile()
print("LangGraph graph compiled.")

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="Banking Assistant — Local API",
    description="Local simulation of API Gateway → AgentCore → LangGraph",
    version="1.0.0",
)


class PromptRequest(BaseModel):
    prompt: str


def _run_graph(prompt: str) -> dict:
    """Core invocation — mirrors the @app.entrypoint in agentcore_langgraph_runtime.py."""
    session_id = str(uuid.uuid4())
    # Reset mock call counter at the start of each new request
    if not USE_LIVE_LLM:
        _mock_instance._reset()
    initial_state: AgentState = {
        "messages": [HumanMessage(content=prompt)]
    }
    final_state = graph.invoke(initial_state)
    answer = final_state["messages"][-1].content
    return {
        "response":          answer,
        "session_id":        session_id,
        "agent_runtime_arn": "arn:aws:bedrock-agentcore:us-east-2:local:runtime/banking_assistant",
        "mode":              "local_mock" if not USE_LIVE_LLM else "aws_bedrock_agentcore",
    }


@app.post("/")
@app.post("/invoke")
async def invoke(request: PromptRequest):
    """
    Main endpoint — mirrors the API Gateway POST in LangGraph.md.

    Body:
        {"prompt": "What is the balance for ACC001?"}
    """
    if not request.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt cannot be empty")
    try:
        result = _run_graph(request.prompt)
        return JSONResponse(content=result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "mode":   "mock" if not USE_LIVE_LLM else "live",
        "graph":  "compiled",
        "tools":  [t.name for t in tools],
    }


@app.get("/accounts")
async def list_accounts():
    """Returns the available test accounts for Postman testing."""
    import json as _json
    with open("data/account_data.json") as f:
        data = _json.load(f)
    return {
        "accounts": [
            {
                "account_id":   acc_id,
                "owner":        acc["owner"],
                "account_type": acc["account_type"],
                "balance":      acc["current_balance"],
                "min_balance":  acc["minimum_balance"],
            }
            for acc_id, acc in data["accounts"].items()
        ]
    }


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"\nBanking Assistant local server starting on http://{args.host}:{args.port}")
    print("Endpoints:")
    print(f"  POST http://localhost:{args.port}/invoke")
    print(f"  GET  http://localhost:{args.port}/health")
    print(f"  GET  http://localhost:{args.port}/accounts")
    print(f"  GET  http://localhost:{args.port}/docs   (Swagger UI)\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
