"""
Supervisor Agent — AgentCore Runtime
=====================================
Inbound:  Cognito JWT (user_client_id) — end-user facing
Outbound: M2M OAuth2 (agent_client_id) via AgentCore Identity → Worker Agent

Features:
  ✅ STM  — MemoryClient.get_last_k_turns / create_event per session
  ✅ LTM  — UserPreference + Summary strategies via retrieve_memories
  ✅ HITL — Human-in-the-Loop gate for high-risk operations (DynamoDB)
  ✅ Routing — banking queries → Worker Agent, general → direct Strands answer

Environment variables:
  WORKER_AGENT_ARN        - Worker (LangGraph) runtime ARN
  AGENTCORE_MEMORY_ID     - Supervisor memory resource ID
  HITL_APPROVAL_TABLE     - DynamoDB table for HITL approvals
  AWS_REGION              - AWS region (default: us-east-2)
"""

import json
import logging
import os
import re
import uuid
from datetime import datetime

import boto3  # type: ignore[import-untyped]
from bedrock_agentcore.runtime import BedrockAgentCoreApp  # type: ignore[import-untyped]
from bedrock_agentcore.identity.auth import requires_access_token  # type: ignore[import-untyped]
from bedrock_agentcore.memory import MemoryClient  # type: ignore[import-untyped]
from strands import Agent
from strands.models import BedrockModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()

# ── Configuration ─────────────────────────────────────────────────────────────
AWS_REGION = os.environ.get("AWS_REGION", "us-east-2")
WORKER_AGENT_ARN = os.environ.get("WORKER_AGENT_ARN", "")
SUPERVISOR_MEMORY_ID = os.environ.get(
    "AGENTCORE_MEMORY_ID", "supervisor_agent_mem-WJH1PRFEwO"
)
HITL_TABLE = os.environ.get("HITL_APPROVAL_TABLE", "banking-hitl-approvals")
MEMORY_TURNS = int(os.environ.get("AGENTCORE_MEMORY_TURNS", "5"))

# LTM namespaces
LTM_PREFS_NS = "/supervisor/preferences/{actor_id}/"
LTM_SUMMARY_NS = "/supervisor/summaries/{actor_id}/{session_id}/"

# ── Model ─────────────────────────────────────────────────────────────────────
_model = BedrockModel(model_id="us.amazon.nova-pro-v1:0")

# ── Memory client ─────────────────────────────────────────────────────────────
_memory_client: MemoryClient | None = None


def _get_memory() -> MemoryClient:
    global _memory_client
    if _memory_client is None:
        _memory_client = MemoryClient(region_name=AWS_REGION)
    return _memory_client


# ── STM helpers ───────────────────────────────────────────────────────────────
def _load_stm(actor_id: str, session_id: str) -> list[dict]:
    """Load prior conversation turns from AgentCore STM."""
    try:
        turns = _get_memory().get_last_k_turns(
            memory_id=SUPERVISOR_MEMORY_ID,
            actor_id=actor_id,
            session_id=session_id,
            k=MEMORY_TURNS,
        )
        messages = []
        for turn in turns:
            for msg in turn:
                role = msg.get("role", "").upper()
                text = msg.get("content", {}).get("text", "")
                if role in ("USER", "ASSISTANT") and text:
                    messages.append({"role": role, "text": text})
        logger.info("[supervisor][stm] Loaded %d messages for session=%s", len(messages), session_id)
        return messages
    except Exception as exc:
        logger.warning("[supervisor][stm] Could not load STM: %s", exc)
        return []


def _save_stm(actor_id: str, session_id: str, user_text: str, assistant_text: str) -> None:
    """Save current turn to AgentCore STM (triggers async LTM extraction)."""
    try:
        _get_memory().create_event(
            memory_id=SUPERVISOR_MEMORY_ID,
            actor_id=actor_id,
            session_id=session_id,
            messages=[(user_text, "USER"), (assistant_text, "ASSISTANT")],
        )
        logger.info("[supervisor][stm] Saved turn for actor=%s session=%s", actor_id, session_id)
    except Exception as exc:
        logger.warning("[supervisor][stm] Could not save turn: %s", exc)


# ── LTM helpers ───────────────────────────────────────────────────────────────
def _load_ltm(actor_id: str, session_id: str) -> str:
    """Load LTM context (preferences + session summaries) for this actor."""
    sections = []
    for label, ns_tpl in [
        ("User preferences", LTM_PREFS_NS),
        ("Session summaries", LTM_SUMMARY_NS),
    ]:
        ns = ns_tpl.format(actor_id=actor_id, session_id=session_id)
        try:
            records = _get_memory().retrieve_memories(
                memory_id=SUPERVISOR_MEMORY_ID,
                namespace=ns,
                query="user preferences and conversation history",
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
            logger.warning("[supervisor][ltm] Could not retrieve %s: %s", ns, exc)

    if not sections:
        return ""
    ltm = "\n\n".join(sections)
    logger.info("[supervisor][ltm] Loaded LTM for actor=%s", actor_id)
    return ltm


# ── AgentCore Identity: M2M token cache ───────────────────────────────────────
_worker_token_cache: dict = {}


@requires_access_token(
    provider_name="WorkerAgent-oauth",
    auth_flow="M2M",
    scopes=[],
)
async def _refresh_worker_token(*, access_token: str) -> None:
    _worker_token_cache["token"] = access_token
    logger.info("[supervisor] Worker M2M token refreshed")


# ── Worker Agent invocation ───────────────────────────────────────────────────
_agentcore_client = None


def _get_agentcore_client():
    global _agentcore_client
    if _agentcore_client is None:
        _agentcore_client = boto3.client("bedrock-agentcore", region_name=AWS_REGION)
    return _agentcore_client


async def _invoke_worker(payload: dict, session_id: str) -> dict:
    """Call the Worker Agent runtime with a valid M2M Bearer token."""
    if not WORKER_AGENT_ARN:
        raise ValueError("WORKER_AGENT_ARN environment variable is not set")

    if "token" not in _worker_token_cache:
        await _refresh_worker_token(access_token="")

    token = _worker_token_cache["token"]
    client = _get_agentcore_client()

    def _inject_bearer(request, **kwargs):
        request.headers["Authorization"] = f"Bearer {token}"

    client.meta.events.register(
        "before-send.bedrock-agentcore.InvokeAgentRuntime", _inject_bearer
    )
    try:
        logger.info("[supervisor] Invoking Worker Agent")
        response = client.invoke_agent_runtime(
            agentRuntimeArn=WORKER_AGENT_ARN,
            runtimeSessionId=session_id,
            qualifier="DEFAULT",
            payload=json.dumps(payload),
        )
        raw = response["response"].read()
        return json.loads(raw)
    finally:
        client.meta.events.unregister(
            "before-send.bedrock-agentcore.InvokeAgentRuntime", _inject_bearer
        )


# ── Intent classification ─────────────────────────────────────────────────────
BANKING_KEYWORDS = {
    "balance", "account", "details", "owner", "penalty",
    "compliance", "minimum", "fee", "acc001", "acc002", "acc003",
    "transaction", "member", "deposit", "withdrawal",
    # Note: "savings", "checking", "banking", "bank" intentionally excluded
    # so general educational questions don't route to the Worker Agent
}

# High-risk operations that require HITL approval
HITL_PATTERNS = [
    "transfer", "wire", "send money", "close account", "delete account",
    "withdraw", "overdraft override", "waive fee", "waive penalty",
    "override", "admin reset",
]


def _is_banking_intent(prompt: str) -> bool:
    words = set(prompt.lower().split())
    has_account_id = bool(re.search(r'\bacc\d{3}\b', prompt.lower()))
    return bool(words & BANKING_KEYWORDS) or has_account_id


def _requires_hitl(prompt: str) -> bool:
    """Return True if the prompt describes a high-risk operation."""
    lower = prompt.lower()
    return any(pattern in lower for pattern in HITL_PATTERNS)


# ── HITL: DynamoDB-backed approval flow ───────────────────────────────────────
_dynamodb = None


def _get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
    return _dynamodb


def _create_hitl_request(actor_id: str, session_id: str, prompt: str) -> tuple[str, str]:
    """
    Write a PENDING approval request to DynamoDB.
    Returns (approval_id, operation_type).
    """
    lower = prompt.lower()
    if any(k in lower for k in ["transfer", "wire", "send money"]):
        op_type = "FUND_TRANSFER"
    elif any(k in lower for k in ["close", "delete"]):
        op_type = "ACCOUNT_CLOSURE"
    elif "withdraw" in lower:
        op_type = "WITHDRAWAL"
    elif any(k in lower for k in ["waive", "override"]):
        op_type = "FEE_OVERRIDE"
    else:
        op_type = "HIGH_RISK_OPERATION"

    approval_id = str(uuid.uuid4())
    try:
        table = _get_dynamodb().Table(HITL_TABLE)
        table.put_item(Item={
            "approval_id": approval_id,
            "actor_id": actor_id,
            "session_id": session_id,
            "prompt": prompt,
            "operation_type": op_type,
            "status": "PENDING",
            "created_at": datetime.utcnow().isoformat(),
            "ttl": int(datetime.utcnow().timestamp()) + 3600,
        })
        logger.info("[hitl] Created approval request: %s (%s)", approval_id, op_type)
    except Exception as exc:
        logger.error("[hitl] Could not create approval request: %s", exc)

    return approval_id, op_type


def _check_hitl_status(approval_id: str) -> str:
    """Check the status of a HITL approval: PENDING | APPROVED | REJECTED."""
    try:
        table = _get_dynamodb().Table(HITL_TABLE)
        resp = table.get_item(Key={"approval_id": approval_id})
        return resp.get("Item", {}).get("status", "PENDING")
    except Exception as exc:
        logger.warning("[hitl] Could not check status: %s", exc)
        return "PENDING"


def _is_pre_approved(approval_id: str | None) -> bool:
    """Return True if a prior approval exists and is APPROVED."""
    if not approval_id:
        return False
    return _check_hitl_status(approval_id) == "APPROVED"


def _hitl_status_check(prompt: str, session_id: str, actor_id: str) -> dict | None:
    """If the prompt asks about an approval status, return the status response."""
    match = re.search(
        r'approval\s+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})',
        prompt.lower(),
    )
    if not match:
        return None

    approval_id = match.group(1)
    status = _check_hitl_status(approval_id)
    msgs = {
        "PENDING":  f"⏳ Approval `{approval_id}` is still **PENDING** review by a supervisor.",
        "APPROVED": f"✅ Approval `{approval_id}` has been **APPROVED**. You may resubmit your request.",
        "REJECTED": f"❌ Approval `{approval_id}` has been **REJECTED**. Contact your account manager.",
    }
    return {
        "result": msgs.get(status, f"Unknown status: {status}"),
        "routed_to": "hitl_status_check",
        "hitl": {"approval_id": approval_id, "status": status},
        "session_id": session_id,
        "actor_id": actor_id,
    }


# ── Supervisor entrypoint ─────────────────────────────────────────────────────
@app.entrypoint
async def handler(payload: dict) -> dict:
    """
    Supervisor entry point.

    Payload:
        {
            "prompt":      "...",
            "account_id":  "ACC001",
            "actor_id":    "testuser",
            "session_id":  "uuid",       # optional
            "approval_id": "uuid"        # optional — for pre-approved HITL ops
        }

    Returns:
        {
            "result":     "...",
            "routed_to":  "worker_agent" | "supervisor_direct" | "hitl_pending" | "hitl_status_check",
            "session_id": "uuid",
            "actor_id":   "...",
            "hitl":       {...}          # only for HITL responses
        }
    """
    prompt: str = payload.get("prompt", "").strip()
    account_id: str = payload.get("account_id", "")
    actor_id: str = payload.get("actor_id") or account_id or "anonymous"
    approval_id: str | None = payload.get("approval_id")

    # Validate session_id (AgentCore requires >= 33 chars)
    raw_session = payload.get("session_id", "")
    session_id = raw_session if (raw_session and len(raw_session) >= 33) else str(uuid.uuid4())

    logger.info("[supervisor] prompt='%s...' actor_id=%s session=%s",
                prompt[:60], actor_id, session_id)

    if not prompt:
        return {"result": "Please provide a prompt.", "routed_to": "supervisor_direct",
                "session_id": session_id, "actor_id": actor_id}

    # ── 1. HITL status check ──────────────────────────────────────────────────
    status_resp = _hitl_status_check(prompt, session_id, actor_id)
    if status_resp:
        return status_resp

    # ── 2. HITL gate — intercept high-risk operations ─────────────────────────
    if _requires_hitl(prompt) and not _is_pre_approved(approval_id):
        logger.warning("[supervisor] HITL required for: %s", prompt[:80])
        approval_id_new, op_type = _create_hitl_request(actor_id, session_id, prompt)

        hitl_result = (
            f"⚠️  **Human Approval Required**\n\n"
            f"The operation you requested ({op_type}) requires supervisor approval "
            f"before it can be executed.\n\n"
            f"**Approval ID:** `{approval_id_new}`\n\n"
            f"**Next steps:**\n"
            f"1. A supervisor will review this request\n"
            f"2. Once approved, resubmit with `approval_id: \"{approval_id_new}\"`\n"
            f"3. To check status, ask: *\"What is the status of approval {approval_id_new}?\"*\n\n"
            f"**Operator commands:**\n"
            f"```\n"
            f"python supervisor/scripts/hitl_approve.py {approval_id_new}\n"
            f"python supervisor/scripts/hitl_reject.py {approval_id_new} 'reason'\n"
            f"python supervisor/scripts/hitl_list.py\n"
            f"```"
        )

        # Save HITL intercept to STM so context is preserved
        _save_stm(actor_id, session_id, prompt, f"[HITL PENDING] {op_type}: {approval_id_new}")

        return {
            "result": hitl_result,
            "routed_to": "hitl_pending",
            "hitl": {
                "approval_id": approval_id_new,
                "operation_type": op_type,
                "status": "PENDING",
            },
            "session_id": session_id,
            "actor_id": actor_id,
        }

    # ── 3. Load STM + LTM context ─────────────────────────────────────────────
    prior_turns = _load_stm(actor_id, session_id)
    ltm_context = _load_ltm(actor_id, session_id)

    # Build context string for the Strands agent system prompt
    context_parts = []
    if ltm_context:
        context_parts.append(f"Long-term memory about this user:\n{ltm_context}")
    if prior_turns:
        history = "\n".join(
            f"  {t['role']}: {t['text'][:150]}" for t in prior_turns[-4:]
        )
        context_parts.append(f"Recent conversation history:\n{history}")

    memory_context = "\n\n".join(context_parts)

    # ── 4. Route to Worker Agent (banking) or answer directly (general) ───────
    if _is_banking_intent(prompt):
        logger.info("[supervisor] Banking intent → Worker Agent")
        try:
            worker_payload = {
                "prompt": prompt,
                "account_id": account_id,
                "actor_id": actor_id,
                "session_id": session_id,
            }
            worker_response = await _invoke_worker(worker_payload, session_id)
            answer = (
                worker_response.get("result")
                or worker_response.get("response")
                or str(worker_response)
            )
        except Exception as exc:
            logger.error("[supervisor] Worker call failed: %s", exc)
            answer = f"I encountered an issue reaching the banking specialist. Error: {str(exc)[:200]}"
            return {
                "result": answer,
                "routed_to": "worker_agent_error",
                "session_id": session_id,
                "actor_id": actor_id,
            }

        # Save routing decision to Supervisor STM
        _save_stm(actor_id, session_id, prompt, answer)

        return {
            "result": answer,
            "routed_to": "worker_agent",
            "session_id": session_id,
            "actor_id": actor_id,
        }

    else:
        # ── General query: Supervisor answers directly ────────────────────────
        logger.info("[supervisor] General intent → direct answer")

        system_prompt = (
            "You are a helpful banking assistant supervisor. "
            "Answer general banking questions directly and helpfully. "
            "For account-specific queries (balances, details, penalties), "
            "let the user know you can look those up if they provide an account ID."
        )
        if memory_context:
            system_prompt += f"\n\nContext from memory:\n{memory_context}"

        try:
            agent = Agent(model=_model, system_prompt=system_prompt)
            response = agent(prompt)
            answer = response.message["content"][0]["text"]
        except Exception as exc:
            logger.error("[supervisor] Direct answer failed: %s", exc)
            answer = f"I'm sorry, I encountered an error: {str(exc)[:200]}"

        # Save to STM
        _save_stm(actor_id, session_id, prompt, answer)

        return {
            "result": answer,
            "routed_to": "supervisor_direct",
            "session_id": session_id,
            "actor_id": actor_id,
        }


if __name__ == "__main__":
    app.run()
