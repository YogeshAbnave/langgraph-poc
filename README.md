# Banking Assistant — LangGraph + AWS Bedrock AgentCore

| Information       | Details                                                    |
|-------------------|------------------------------------------------------------|
| Agent type        | Synchronous                                                |
| Framework         | LangGraph                                                  |
| LLM model         | Amazon Nova Pro (`us.amazon.nova-pro-v1:0`)                |
| Components        | AgentCore Runtime, Lambda Tools, JSON Knowledge Base       |
| Region            | us-east-2                                                  |
| SDK               | Amazon BedrockAgentCore Python SDK                         |

---

## Architecture

```
User / Client
    │  POST {"prompt": "What is the balance for ACC001?"}
    ▼
API Gateway
    │
    ▼
Lambda: api-gateway-invoker
    │  generates session_id, calls InvokeAgentRuntime
    ▼
AWS Bedrock AgentCore Runtime
    │
    ▼  agentcore_langgraph_runtime.py
    │
    ├── START
    │     │
    │     ▼
    │  call_model  ◄─────────────────────┐
    │  (Nova Pro)                         │
    │     │                               │
    │  should_continue?                   │
    │     ├── "end"  ──► END              │
    │     └── "continue"                  │
    │           │                         │
    │           ▼                         │
    │        tools node ──────────────────┘
    │        ├── get_account_balance
    │        └── get_account_details
    │
    ▼
Lambda Functions
    ├── get-account-balance  → loads account_data.json + penalty_rules.json
    └── get-account-details  → loads account_data.json
```

---

## Project Structure

```
.
├── agentcore_langgraph_runtime.py   # Main entrypoint — LangGraph + AgentCore
├── local_server.py                  # Local FastAPI server for Postman testing
├── tools/
│   ├── __init__.py
│   └── banking_tools.py             # @tool definitions (Lambda dispatch)
├── lambda_handlers/
│   ├── get_account_balance.py       # Computes balance + penalty compliance (local)
│   └── get_account_details.py       # Returns owner info (local)
├── deploy/
│   ├── _create_apigw.py             # One-shot script to create API Gateway + Lambda integration
│   ├── _deploy_invoker.py           # Deploys api-gateway-invoker Lambda and adds API Gateway permission
│   ├── _deploy_tools.py             # Deploys get-account-balance and get-account-details Lambdas
│   ├── _verify_lambdas.py           # Smoke-tests deployed Lambda functions directly via boto3
│   ├── lambda/
│   │   ├── api_gateway_invoker/
│   │   │   └── handler.py           # API Gateway → AgentCore invoker Lambda
│   │   ├── get_account_balance/
│   │   │   └── handler.py           # Deployment-ready Lambda package handler
│   │   └── get_account_details/
│   │       └── handler.py           # Deployment-ready Lambda package handler
│   └── postman/
│       └── banking_assistant.postman_collection.json  # Postman test collection
├── data/
│   ├── account_data.json            # Account records (mock data)
│   └── penalty_rules.json           # Minimum balance + fee rules
├── scripts/
│   └── setup_memory.py              # One-time script to provision AgentCore Memory resource
├── tests/
│   └── test_local.py                # Local test suite (no AWS required)
├── requirements.txt
└── README.md
```

---

## Key Components

### AgentState
```python
class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], operator.add]
```

### LLM
```python
llm = ChatBedrock(
    model_id="us.amazon.nova-pro-v1:0",
    client=bedrock_runtime,
    model_kwargs={"temperature": 0.1, "max_tokens": 2000}
)
```

### Graph Nodes

| Node         | Role                                                        |
|--------------|-------------------------------------------------------------|
| `call_model` | LLM reasoning — decides which tool(s) to call              |
| `tools`      | Executes `get_account_balance` or `get_account_details`     |

### AgentCore Memory (STM + LTM)

The runtime uses `MemoryClient` from `bedrock_agentcore.memory` to persist conversation history and long-term user context across invocations.

```python
from bedrock_agentcore.memory import MemoryClient

memory_client = MemoryClient(region_name=AWS_REGION)
```

On each invocation the entrypoint:

1. Calls `memory_client.get_last_k_turns(memory_id, actor_id, session_id, k=10)` to restore prior conversation turns as `HumanMessage` / `AIMessage` objects (STM).
2. Prepends those messages to the LangGraph initial state so the LLM has full context.
3. After the graph completes, calls `memory_client.create_event(...)` to persist the new user→assistant turn. AgentCore automatically runs LTM extraction strategies asynchronously to update the actor's long-term memory namespaces.

**LTM namespaces** (populated automatically by AgentCore after each `create_event`):

| Namespace | Content |
|---|---|
| `/banking/preferences/{actor_id}/` | User preferences extracted from conversation |
| `/banking/facts/{actor_id}/` | Semantic banking facts about the user |

LTM records are retrieved at invocation start via `_load_ltm_context(actor_id)` and can be injected as a system message prefix so the LLM is aware of the user's known preferences and previously extracted facts.

**Identity fields:**

| Field      | Source                                                                 |
|------------|------------------------------------------------------------------------|
| `actor_id` | `payload["actor_id"]` → `payload["account_id"]` (from JWT) → `"anonymous"` |
| `session_id` | `context.session_id` → `payload["session_id"]` → `"default-session"` |

**Environment variables:**

| Variable                | Default                                    | Description                              |
|-------------------------|--------------------------------------------|------------------------------------------|
| `AGENTCORE_MEMORY_ID`   | `agentcore_langgraph_runtime_mem-EhLa086Zic` | Memory resource ID from `.bedrock_agentcore.yaml` |
| `AGENTCORE_MEMORY_TURNS`| `10`                                       | Number of prior turns to reload          |

### Tools

| Tool                   | Lambda Function        | Data Sources                              |
|------------------------|------------------------|-------------------------------------------|
| `get_account_balance`  | `get-account-balance`  | `account_data.json`, `penalty_rules.json` |
| `get_account_details`  | `get-account-details`  | `account_data.json`                       |

---

## Setup

```bash
pip install uv
uv venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
uv pip install -r requirements.txt
aws configure
```

---

## Local Testing

Run the full test suite locally — no AWS credentials or Lambda required:

```bash
python tests/test_local.py
```

The test suite covers:

- Lambda handler logic (`get_account_balance`, `get_account_details`) with known accounts, edge cases, and error paths
- `@tool` functions with local dispatch (`USE_LAMBDA=false`)
- Full LangGraph graph execution with a mocked LLM (tool call flow + direct answer flow)
- Knowledge base data integrity (`account_data.json`, `penalty_rules.json`)

---

## Local Development

### Postman Collection

A ready-to-use Postman collection is included at `deploy/postman/banking_assistant.postman_collection.json`.

**Setup:**
1. Import the collection into Postman.
2. Set the `base_url` collection variable to your API Gateway endpoint (e.g. `https://abc123.execute-api.us-east-2.amazonaws.com`) — or `http://localhost:8000` when testing against `local_server.py`.

**Included requests:**

| Folder | Requests |
|---|---|
| Health Check | GET `/health` (local only) |
| Account Balance Queries | Balance for ACC001, ACC002, ACC003 |
| Account Details Queries | Owner/contact info for ACC001, ACC002 |
| Combined Queries | Balance + details in one call; multi-account penalty check |
| Multi-Turn Conversation | Two-turn session using `session_id` |
| Error Cases | Invalid account ID (ACC999), empty prompt (expects 400) |

Test scripts are included on key requests to assert status codes, validate response fields, and auto-save `session_id` for multi-turn flows.

---

### Local API Server (Postman / HTTP testing)

`local_server.py` spins up a FastAPI server that mirrors the full API Gateway → AgentCore → LangGraph flow locally.

**Mock mode** (default — no AWS credentials needed):

```bash
python local_server.py
```

**Live mode** (real AWS Bedrock / Nova Pro):

```bash
python local_server.py --live
```

Optional flags: `--host 0.0.0.0 --port 8000`

Endpoints:

| Method | Path        | Description                          |
|--------|-------------|--------------------------------------|
| POST   | `/`         | Main invoke endpoint                 |
| POST   | `/invoke`   | Alias for `/`                        |
| GET    | `/health`   | Health check + mode info             |
| GET    | `/accounts` | List available test accounts         |
| GET    | `/docs`     | Swagger UI                           |

Example request:

```bash
curl -X POST http://localhost:8000/invoke \
  -H "Content-Type: application/json" \
  -d '{"prompt": "What is the balance for ACC001?"}'
```

Example response:

```json
{
  "response": "The current balance for account ACC001 is $1,200.50 USD...",
  "session_id": "d3c959c4-...",
  "agent_runtime_arn": "arn:aws:bedrock-agentcore:us-east-2:local:runtime/banking_assistant",
  "mode": "local_mock"
}
```

### Direct Runtime (no HTTP layer)

Run without deploying to Lambda (handlers called directly):

```bash
# USE_LAMBDA defaults to false — Lambda handlers are imported locally
python agentcore_langgraph_runtime.py
```

To switch to real Lambda invocation:

```bash
export USE_LAMBDA=true
export LAMBDA_BALANCE_FN=get-account-balance
export LAMBDA_DETAILS_FN=get-account-details
export AWS_REGION=us-east-2
python agentcore_langgraph_runtime.py
```

---

## Deploy with AgentCore

```bash
agentcore configure
agentcore launch -e agentcore_langgraph_runtime.py
```

### API Gateway Setup

`deploy/_create_apigw.py` is a one-shot script that creates the HTTP API Gateway wired to the `api-gateway-invoker` Lambda. Run it once after the Lambda functions are deployed:

```bash
python deploy/_create_apigw.py
```

It creates:
- An HTTP API (`banking-assistant-api`) with a `POST /` route
- A `$default` stage with auto-deploy enabled
- A Lambda resource-based policy granting API Gateway invoke permission

Update the `PROFILE`, `ACCOUNT`, and `AGENT_ARN` constants at the top of the script before running.

### Deploying the Tool Lambdas

`deploy/_deploy_tools.py` packages and deploys the `get-account-balance` and `get-account-details` Lambda functions. Each zip includes the handler and the `data/` JSON files. It also creates the `banking-lambda-tools-role` IAM role if it doesn't already exist.

```bash
python deploy/_deploy_tools.py
```

Update the `PROFILE`, `REGION`, and `ACCOUNT` constants at the top of the script before running.

### Verifying Deployed Lambdas

`deploy/_verify_lambdas.py` is a quick smoke-test script that invokes the deployed `get-account-balance` and `get-account-details` Lambda functions directly via boto3 and prints the results. Useful for confirming the functions are live and returning expected data after a deployment.

```bash
python deploy/_verify_lambdas.py
```

Update the `profile_name` at the top of the script to match your AWS CLI profile before running.

### Deploying the Invoker Lambda

`deploy/_deploy_invoker.py` packages and deploys the `api-gateway-invoker` Lambda function, then adds the API Gateway resource-based permission in one step:

```bash
python deploy/_deploy_invoker.py
```

It will create the Lambda if it doesn't exist, or update the code and environment variables if it does. Update the `PROFILE`, `ACCOUNT`, `AGENT_ARN`, and `API_ID` constants at the top of the script before running.

### Lambda Deployment Packages

The `deploy/lambda/` directory contains self-contained handlers for each Lambda function:

| Directory                  | Function               | Description                                                  |
|----------------------------|------------------------|--------------------------------------------------------------|
| `api_gateway_invoker/`     | `api-gateway-invoker`  | Receives API Gateway requests, generates `session_id`, invokes AgentCore Runtime via `bedrock-agentcore:InvokeAgentRuntime`. Requires `AGENT_RUNTIME_ARN` env var. |
| `get_account_balance/`     | `get-account-balance`  | Computes balance and penalty compliance from the knowledge base. Bundle with `data/` JSON files. |
| `get_account_details/`     | `get-account-details`  | Returns account owner info from the knowledge base. Bundle with `data/` JSON files. |

The `api_gateway_invoker` handler supports both API Gateway proxy events and direct Lambda invocations, and accepts an optional `session_id` in the request body for multi-turn conversations.

### Test

```bash
agentcore invoke {"prompt":"What is the balance for ACC001?"}
agentcore invoke {"prompt":"Give me my current account balance and minimum balance requirements for ACC002"}
```

### Cleanup

```bash
agentcore destroy
```

---

## Message Flow Example

**Query:** `"What is the balance for ACC001?"`

1. `HumanMessage`: "What is the balance for ACC001?"
2. `AIMessage`: `[tool_call: get_account_balance(account_id="ACC001")]`
3. `ToolMessage`: `{"account_id": "ACC001", "current_balance": 1200.50, "minimum_balance_required": 2500.00, "is_compliant": false, "penalty_exposure": 25.00, ...}`
4. `AIMessage`: "The balance for account ACC001 is $1,200.50. The account is below the $2,500 minimum for Premium Checking and is subject to a $25 monthly fee."

---

## Extending

- **Add a tool**: define `@tool` in `tools/banking_tools.py`, add a handler in `lambda_handlers/`, append to `tools` list in the runtime.
- **Swap knowledge base**: replace JSON files with a real DB or vector store — Lambda handler interfaces stay the same.
- **Add nodes**: insert new nodes (e.g. `fraud_check`) into the graph before `call_model`.

---

## IAM Permissions Required

### AgentCore Runtime Role
- `bedrock:InvokeModel` — for Nova Pro
- `lambda:InvokeFunction` — for tool Lambdas
- CloudWatch Logs write access


