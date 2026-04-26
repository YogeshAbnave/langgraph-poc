import logging
import operator
import os
from typing import Annotated, Sequence

import boto3
from langchain_aws import ChatBedrock
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from typing_extensions import TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode

# ---------------------------------------------------------------------------
# Logging / telemetry
# ---------------------------------------------------------------------------
logging.getLogger("langchain").setLevel(logging.DEBUG)
os.environ["LANGSMITH_OTEL_ENABLED"] = "true"
print("Starting banking assistant runtime...")

# ---------------------------------------------------------------------------
# LLM — Amazon Nova Pro via ChatBedrock (per LangGraph.md spec)
# ---------------------------------------------------------------------------
AWS_REGION = os.environ.get("AWS_REGION", "us-east-2")

bedrock_runtime = boto3.client(
    service_name="bedrock-runtime",
    region_name=AWS_REGION,
)

llm = ChatBedrock(
    model_id="us.amazon.nova-pro-v1:0",
    client=bedrock_runtime,
    model_kwargs={"temperature": 0.1, "max_tokens": 2000},
)

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
from tools.banking_tools import get_account_balance, get_account_details

tools = [get_account_balance, get_account_details]
llm_with_tools = llm.bind_tools(tools)

# ---------------------------------------------------------------------------
# Agent State
# ---------------------------------------------------------------------------

class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], operator.add]

# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------

def call_model(state: AgentState) -> dict:
    """
    Agent node: invoke the LLM with the current message history.
    The LLM decides whether to call a tool or produce a final answer.
    """
    messages = state["messages"]
    response = llm_with_tools.invoke(messages)
    return {"messages": [response]}


def should_continue(state: AgentState) -> str:
    """
    Decision node: route to 'tools' if the LLM emitted tool calls,
    otherwise route to END.
    """
    last_message = state["messages"][-1]
    if not last_message.tool_calls:
        return "end"
    return "continue"

# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------
print("Configuring LangGraph state graph...")

graph_builder = StateGraph(AgentState)

# Nodes
graph_builder.add_node("call_model", call_model)
graph_builder.add_node("tools", ToolNode(tools=tools))

# Edges
graph_builder.add_edge(START, "call_model")

graph_builder.add_conditional_edges(
    "call_model",
    should_continue,
    {
        "continue": "tools",   # tool calls detected → execute tools
        "end":      END,       # no tool calls → return final answer
    },
)

# After tools execute, loop back to the agent for synthesis
graph_builder.add_edge("tools", "call_model")

graph = graph_builder.compile()
print("LangGraph state graph compiled successfully.")

# ---------------------------------------------------------------------------
# AgentCore Memory client (STM + LTM)
# ---------------------------------------------------------------------------
from bedrock_agentcore.memory import MemoryClient

# Memory resource ID from .bedrock_agentcore.yaml (agentcore_langgraph_runtime agent).
# Can be overridden via environment variable for flexibility across environments.
MEMORY_ID = os.environ.get(
    "AGENTCORE_MEMORY_ID",
    "agentcore_langgraph_runtime_mem-EhLa086Zic",
)

# How many prior conversation turns (STM) to reload at the start of each invocation.
# Each "turn" = one user message + one assistant reply.
MEMORY_TURNS_TO_LOAD = int(os.environ.get("AGENTCORE_MEMORY_TURNS", "10"))

# LTM namespace templates — must match the namespaces defined in scripts/setup_memory.py.
LTM_PREFERENCES_NS = "/banking/preferences/{actor_id}/"
LTM_FACTS_NS       = "/banking/facts/{actor_id}/"

memory_client = MemoryClient(region_name=AWS_REGION)
print(f"AgentCore Memory client initialised (memory_id={MEMORY_ID})")


def _load_prior_messages(actor_id: str, session_id: str) -> list[BaseMessage]:
    """
    Retrieve the last MEMORY_TURNS_TO_LOAD conversation turns from AgentCore
    STM and convert them to LangChain BaseMessage objects so LangGraph can
    replay the full conversation history.

    Each turn returned by get_last_k_turns is a list of dicts:
        {"role": "USER" | "ASSISTANT", "content": {"text": "..."}}

    Tool messages are not stored in AgentCore STM (they are ephemeral
    within a single graph execution), so only USER / ASSISTANT turns
    are restored here.
    """
    try:
        turns = memory_client.get_last_k_turns(
            memory_id=MEMORY_ID,
            actor_id=actor_id,
            session_id=session_id,
            k=MEMORY_TURNS_TO_LOAD,
        )
    except Exception as exc:
        # Non-fatal: if memory retrieval fails, start fresh rather than
        # crashing the agent.
        print(f"[memory] WARNING: could not load prior turns: {exc}")
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
            # TOOL role messages are skipped — they are transient and
            # not meaningful without the corresponding tool call metadata.

    print(f"[memory] Loaded {len(messages)} prior messages for session={session_id}")
    return messages


def _load_ltm_context(actor_id: str) -> str:
    """
    Retrieve Long-Term Memory records for this actor from both LTM namespaces:
      - UserPreference  (/banking/preferences/{actor_id}/)
      - Semantic/Facts  (/banking/facts/{actor_id}/)

    Returns a formatted string to inject as a system message prefix so the
    LLM is aware of the user's known preferences and previously extracted
    banking facts.  Returns an empty string if no LTM records exist or if
    retrieval fails (non-fatal).
    """
    sections: list[str] = []

    for label, namespace_template in [
        ("User preferences", LTM_PREFERENCES_NS),
        ("Known banking facts", LTM_FACTS_NS),
    ]:
        namespace = namespace_template.format(actor_id=actor_id)
        try:
            records = memory_client.retrieve_memories(
                memory_id=MEMORY_ID,
                namespace=namespace,
            )
            if records:
                items = [
                    r.get("content", {}).get("text", "") or str(r)
                    for r in records
                    if r
                ]
                items = [i for i in items if i.strip()]
                if items:
                    sections.append(f"{label}:\n" + "\n".join(f"- {i}" for i in items))
        except Exception as exc:
            # LTM retrieval is best-effort — don't crash the agent.
            print(f"[memory] WARNING: could not retrieve LTM ({namespace}): {exc}")

    if not sections:
        return ""

    ltm_text = "\n\n".join(sections)
    print(f"[memory] Loaded LTM context for actor={actor_id} ({len(sections)} section(s))")
    return ltm_text


def _save_turn(actor_id: str, session_id: str, human_text: str, assistant_text: str) -> None:
    """
    Persist the current user→assistant exchange to AgentCore Memory via
    create_event.  AgentCore automatically:
      - Stores the raw event in STM for session continuity
      - Runs LTM extraction strategies asynchronously (UserPreference + Semantic)
        to update the actor's long-term memory namespaces

    Storing both messages in a single event keeps them grouped as one logical
    turn, which is what get_last_k_turns expects.
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
        print(f"[memory] Saved turn to session={session_id}, actor={actor_id}")
    except Exception as exc:
        # Non-fatal: log and continue — the agent response is still returned.
        print(f"[memory] WARNING: could not save turn: {exc}")


# ---------------------------------------------------------------------------
# Bedrock AgentCore entrypoint
# ---------------------------------------------------------------------------
from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()


@app.entrypoint
def agent_invocation(payload, context):
    """
    Entry point called by AgentCore Runtime.

    Expected payload:
        {
            "prompt":     "What is the balance for ACC001?",
            "account_id": "ACC001",          # injected by api-gateway-invoker from JWT
            "actor_id":   "user-123"         # optional; falls back to account_id or "anonymous"
        }

    Returns:
        {
            "result":            "<agent answer>",
            "session_id":        "<uuid>",
            "agent_runtime_arn": "<arn>",
            "mode":              "aws_bedrock_agentcore"
        }
    """
    print("Received payload:", payload)

    prompt = payload.get(
        "prompt",
        "No prompt provided. Please ask a banking-related question.",
    )

    # ── Identity resolution ──────────────────────────────────────────────────
    # actor_id uniquely identifies the user across sessions (used as the memory
    # namespace key).  Prefer an explicit actor_id; fall back to account_id
    # (which the invoker Lambda extracts from the JWT claim), then "anonymous".
    actor_id: str = (
        payload.get("actor_id")
        or payload.get("account_id")
        or "anonymous"
    )

    # session_id groups a multi-turn conversation.  AgentCore injects it via
    # context; the invoker Lambda also passes it in the payload for continuity.
    session_id: str = (
        getattr(context, "session_id", None)
        or payload.get("session_id")
        or "default-session"
    )

    print(f"[memory] actor_id={actor_id}  session_id={session_id}")

    # ── Step 1: Restore conversation history from AgentCore STM ─────────────
    prior_messages = _load_prior_messages(actor_id, session_id)

    # ── Step 1b: Load Long-Term Memory context (preferences + facts) ─────────
    # LTM records are injected as a system message so the LLM is aware of the
    # user's known preferences and previously extracted banking facts without
    # those details needing to appear in the raw conversation history.
    ltm_context = _load_ltm_context(actor_id)
    system_messages: list[BaseMessage] = []
    if ltm_context:
        from langchain_core.messages import SystemMessage
        system_messages = [SystemMessage(content=(
            "You are a helpful banking assistant. "
            "Use the following long-term memory about this user to personalise your responses:\n\n"
            + ltm_context
        ))]

    # ── Step 2: Run LangGraph with full history + new user message ───────────
    initial_state: AgentState = {
        "messages": system_messages + prior_messages + [HumanMessage(content=prompt)]
    }

    final_state = graph.invoke(initial_state)
    final_answer = final_state["messages"][-1].content
    print("Final answer:", final_answer)

    # ── Step 3: Persist this turn to AgentCore STM ───────────────────────────
    _save_turn(actor_id, session_id, prompt, final_answer)

    # ── Step 4: Build response ───────────────────────────────────────────────
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
