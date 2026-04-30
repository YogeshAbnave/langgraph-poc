"""
Worker Agent — AgentCore Runtime (LangGraph)
=============================================
Inbound:  Cognito JWT (agent_client_id ONLY) — called by Supervisor via M2M
Outbound: boto3 Lambda.invoke → get-account-balance, get-account-details

Features:
  ✅ STM  — AgentCoreMemorySessionManager restores conversation history per session
  ✅ LTM  — UserPreference + Semantic strategies persist facts across sessions
  ✅ LangGraph state graph — call_model → tools → call_model loop
  ✅ Lambda tools — real AWS Lambda functions (USE_LAMBDA=true in production)

Environment variables:
  AGENTCORE_MEMORY_ID   - Worker memory resource ID
  AGENTCORE_MEMORY_TURNS - Number of prior turns to reload (default: 10)
  USE_LAMBDA            - 'true' to call real Lambda, 'false' for local (default: false)
  LAMBDA_BALANCE_FN     - Lambda function name for balance queries
  LAMBDA_DETAILS_FN     - Lambda function name for details queries
  AWS_REGION            - AWS region (default: us-east-2)
"""

import logging
import operator
import os
from typing import Annotated, Sequence

import boto3
from langchain_aws import ChatBedrock
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from typing_extensions import TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode

from bedrock_agentcore.memory import MemoryClient

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("langchain").setLevel(logging.WARNING)
os.environ["LANGSMITH_OTEL_ENABLED"] = "true"
print("Starting Worker Agent (LangGraph banking assistant)...")

# ── Configuration ─────────────────────────────────────────────────────────────
AWS_REGION = os.environ.get("AWS_REGION", "us-east-2")

MEMORY_ID = os.environ.get(
    "AGENTCORE_MEMORY_ID",
    os.environ.get(
        "BEDROCK_AGENTCORE_MEMORY_ID",
        "agentcore_langgraph_runtime_mem-EhLa086Zic",
    ),
)
MEMORY_TURNS_TO_LOAD = int(os.environ.get("AGENTCORE_MEMORY_TURNS", "10"))

# LTM namespace templates (must match setup_memory.py)
LTM_PREFERENCES_NS = "/banking/preferences/{actor_id}/"
LTM_FACTS_NS = "/banking/facts/{actor_id}/"

# ── LLM ───────────────────────────────────────────────────────────────────────
bedrock_runtime_client = boto3.client("bedrock-runtime", region_name=AWS_REGION)

llm = ChatBedrock(
    model_id="us.amazon.nova-pro-v1:0",
    client=bedrock_runtime_client,
    model_kwargs={"temperature": 0.1, "max_tokens": 2000},
)

# ── Tools ─────────────────────────────────────────────────────────────────────
from tools.banking_tools import get_account_balance, get_account_details

tools = [get_account_balance, get_account_details]
llm_with_tools = llm.bind_tools(tools)

# ── Agent State ───────────────────────────────────────────────────────────────
class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], operator.add]


# ── Graph nodes ───────────────────────────────────────────────────────────────
def call_model(state: AgentState) -> dict:
    """Invoke the LLM. It decides whether to call a tool or produce a final answer."""
    response = llm_with_tools.invoke(state["messages"])
    return {"messages": [response]}


def should_continue(state: AgentState) -> str:
    """Route to 'tools' if the LLM emitted tool calls, otherwise END."""
    last = state["messages"][-1]
    if not getattr(last, "tool_calls", None):
        return "end"
    return "continue"


# ── Graph construction ────────────────────────────────────────────────────────
print("Compiling LangGraph state graph...")
graph_builder = StateGraph(AgentState)
graph_builder.add_node("call_model", call_model)
graph_builder.add_node("tools", ToolNode(tools=tools))
graph_builder.add_edge(START, "call_model")
graph_builder.add_conditional_edges(
    "call_model",
    should_continue,
    {"continue": "tools", "end": END},
)
graph_builder.add_edge("tools", "call_model")
graph = graph_builder.compile()
print("LangGraph state graph compiled.")

# ── Memory client (for STM get_last_k_turns + LTM retrieve_memories) ─────────
memory_client = MemoryClient(region_name=AWS_REGION)
print(f"AgentCore Memory client ready (memory_id={MEMORY_ID})")


# ── STM: load prior conversation turns ───────────────────────────────────────
def _load_stm(actor_id: str, session_id: str) -> list[BaseMessage]:
    """
    Restore the last N conversation turns from AgentCore STM.
    Converts USER/ASSISTANT records back to LangChain message objects.
    """
    try:
        turns = memory_client.get_last_k_turns(
            memory_id=MEMORY_ID,
            actor_id=actor_id,
            session_id=session_id,
            k=MEMORY_TURNS_TO_LOAD,
        )
    except Exception as exc:
        logger.warning("[worker][stm] Could not load prior turns: %s", exc)
        return []

    messages: list[BaseMessage] = []
    for turn in turns:
        for msg in turn:
            role = msg.get("role", "").upper()
            text = msg.get("content", {}).get("text", "")
            if role == "USER":
                messages.append(HumanMessage(content=text))
            elif role == "ASSISTANT":
                messages.append(AIMessage(content=text))

    logger.info("[worker][stm] Loaded %d prior messages for session=%s", len(messages), session_id)
    return messages


# ── LTM: load long-term memory context ───────────────────────────────────────
def _load_ltm(actor_id: str) -> str:
    """
    Retrieve LTM records from both namespaces:
      - /banking/preferences/{actor_id}/  → user preferences
      - /banking/facts/{actor_id}/        → extracted banking facts

    Returns a formatted string injected as a SystemMessage so the LLM
    personalises responses without polluting the conversation history.
    """
    sections: list[str] = []

    for label, ns_template in [
        ("User preferences", LTM_PREFERENCES_NS),
        ("Known banking facts", LTM_FACTS_NS),
    ]:
        namespace = ns_template.format(actor_id=actor_id)
        try:
            records = memory_client.retrieve_memories(
                memory_id=MEMORY_ID,
                namespace=namespace,
            )
            if records:
                items = [
                    r.get("content", {}).get("text", "") or str(r)
                    for r in records if r
                ]
                items = [i for i in items if i.strip()]
                if items:
                    sections.append(f"{label}:\n" + "\n".join(f"  - {i}" for i in items))
        except Exception as exc:
            logger.warning("[worker][ltm] Could not retrieve %s: %s", namespace, exc)

    if not sections:
        return ""

    ltm_text = "\n\n".join(sections)
    logger.info("[worker][ltm] Loaded LTM for actor=%s (%d sections)", actor_id, len(sections))
    return ltm_text


# ── STM: save current turn ────────────────────────────────────────────────────
def _save_stm(actor_id: str, session_id: str, human_text: str, assistant_text: str) -> None:
    """
    Persist the current user→assistant exchange to AgentCore Memory.
    AgentCore automatically:
      - Stores the raw event in STM for session continuity
      - Runs LTM extraction asynchronously (UserPreference + Semantic strategies)
    """
    try:
        memory_client.create_event(
            memory_id=MEMORY_ID,
            actor_id=actor_id,
            session_id=session_id,
            messages=[
                (human_text, "USER"),
                (assistant_text, "ASSISTANT"),
            ],
        )
        logger.info("[worker][stm] Saved turn — session=%s actor=%s", session_id, actor_id)
    except Exception as exc:
        logger.warning("[worker][stm] Could not save turn (non-fatal): %s", exc)


# ── AgentCoreMemorySessionManager not used in Worker (LangGraph uses raw MemoryClient)
# The Worker uses MemoryClient directly for STM/LTM — this is the correct pattern
# for LangGraph agents. AgentCoreMemorySessionManager is Strands-specific.


# ── AgentCore entrypoint ──────────────────────────────────────────────────────
from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()


@app.entrypoint
def agent_invocation(payload: dict, context) -> dict:
    """
    Worker Agent entry point called by AgentCore Runtime.

    Expected payload (from Supervisor Agent):
        {
            "prompt":     "What is the balance for ACC001?",
            "account_id": "ACC001",
            "actor_id":   "testuser",
            "session_id": "uuid-..."
        }

    Returns:
        {
            "result":     "<agent answer>",
            "session_id": "<uuid>",
            "actor_id":   "<actor_id>",
            "mode":       "aws_bedrock_agentcore"
        }
    """
    logger.info("[worker] Received payload keys: %s", list(payload.keys()))

    prompt: str = payload.get(
        "prompt",
        "No prompt provided. Please ask a banking-related question.",
    )

    # ── Identity resolution ───────────────────────────────────────────────────
    actor_id: str = (
        payload.get("actor_id")
        or payload.get("account_id")
        or "anonymous"
    )
    session_id: str = (
        getattr(context, "session_id", None)
        or payload.get("session_id")
        or "default-session"
    )

    logger.info("[worker] actor_id=%s  session_id=%s", actor_id, session_id)

    # ── Step 1: Load STM — restore prior conversation turns ───────────────────
    prior_messages = _load_stm(actor_id, session_id)

    # ── Step 2: Load LTM — inject user preferences + facts as SystemMessage ──
    ltm_context = _load_ltm(actor_id)
    system_messages: list[BaseMessage] = []
    if ltm_context:
        system_messages = [SystemMessage(content=(
            "You are a helpful banking assistant with access to account data tools.\n"
            "Use the following long-term memory about this user to personalise your responses:\n\n"
            + ltm_context
        ))]
    else:
        system_messages = [SystemMessage(content=(
            "You are a helpful banking assistant with access to account data tools. "
            "Use get_account_balance to check balances and compliance. "
            "Use get_account_details to get owner information. "
            "Always be accurate and professional."
        ))]

    # ── Step 3: Run LangGraph with full history + new user message ────────────
    initial_state: AgentState = {
        "messages": system_messages + prior_messages + [HumanMessage(content=prompt)]
    }

    logger.info(
        "[worker] Running LangGraph — %d prior messages + 1 new",
        len(prior_messages),
    )
    final_state = graph.invoke(initial_state)
    final_answer = final_state["messages"][-1].content
    logger.info("[worker] Final answer: %s...", str(final_answer)[:100])

    # ── Step 4: Save turn to STM (triggers async LTM extraction) ─────────────
    _save_stm(actor_id, session_id, prompt, final_answer)

    # ── Step 5: Build response ────────────────────────────────────────────────
    response = {
        "result": final_answer,
        "mode": "aws_bedrock_agentcore",
        "session_id": session_id,
        "actor_id": actor_id,
    }
    if hasattr(context, "agent_runtime_arn"):
        response["agent_runtime_arn"] = context.agent_runtime_arn

    return response


app.run()
