# Deployment Guide — Banking Assistant on AWS

## Architecture

```
Postman
  │
  │  POST {"prompt": "..."}
  ▼
API Gateway (HTTP API)
  │
  │  Lambda proxy
  ▼
Lambda: api-gateway-invoker
  │  - Parses prompt
  │  - Generates session_id
  │  - Calls bedrock-agentcore:InvokeAgentRuntime
  ▼
AgentCore Runtime (banking_assistant)
  │  agentcore_langgraph_runtime.py
  │  LangGraph: START → call_model → tools → call_model → END
  ▼
Lambda: get-account-balance   Lambda: get-account-details
  │  data/                         │  data/
  │  account_data.json             │  account_data.json
  │  penalty_rules.json            │
  └──────────────────────────────────┘
```

---

## Prerequisites

```bash
# 1. AWS CLI configured
aws configure
aws sts get-caller-identity   # verify credentials

# 2. Python 3.10+ with uv
pip install uv
uv venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Install dependencies
uv pip install -r requirements.txt

# 4. Install AgentCore CLI
pip install bedrock-agentcore-starter-toolkit

# 5. Verify agentcore CLI
agentcore --version
```

---

## Deployment Steps

### Option A — Automated (recommended)

```bash
chmod +x deploy/deploy.sh
./deploy/deploy.sh
```

The script handles all steps below automatically.

---

### Option B — Manual Step-by-Step

#### Step 1 — Create IAM Roles

```bash
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
AWS_REGION=us-east-2

# Role for tool Lambda functions (get-account-balance, get-account-details)
aws iam create-role \
  --role-name banking-lambda-tools-role \
  --assume-role-policy-document file://deploy/iam/lambda_tool_trust_policy.json

aws iam put-role-policy \
  --role-name banking-lambda-tools-role \
  --policy-name banking-lambda-tools-policy \
  --policy-document file://deploy/iam/lambda_tool_permissions.json

# Role for api-gateway-invoker Lambda
aws iam create-role \
  --role-name banking-lambda-invoker-role \
  --assume-role-policy-document file://deploy/iam/lambda_tool_trust_policy.json

aws iam put-role-policy \
  --role-name banking-lambda-invoker-role \
  --policy-name banking-lambda-invoker-policy \
  --policy-document file://deploy/iam/lambda_invoker_permissions.json

# Role for AgentCore Runtime
aws iam create-role \
  --role-name banking-agentcore-runtime-role \
  --assume-role-policy-document file://deploy/iam/agentcore_runtime_trust_policy.json

aws iam put-role-policy \
  --role-name banking-agentcore-runtime-role \
  --policy-name banking-agentcore-runtime-policy \
  --policy-document file://deploy/iam/agentcore_runtime_permissions.json

sleep 10   # wait for IAM propagation
```

#### Step 2 — Package and Deploy Tool Lambdas

```bash
# get-account-balance
cd deploy/lambda/get_account_balance
zip -r /tmp/get-account-balance.zip .
cd ../../..
zip -r /tmp/get-account-balance.zip data/

aws lambda create-function \
  --function-name get-account-balance \
  --runtime python3.12 \
  --role arn:aws:iam::${AWS_ACCOUNT_ID}:role/banking-lambda-tools-role \
  --handler handler.handler \
  --zip-file fileb:///tmp/get-account-balance.zip \
  --timeout 30 \
  --memory-size 256 \
  --region $AWS_REGION

# get-account-details
cd deploy/lambda/get_account_details
zip -r /tmp/get-account-details.zip .
cd ../../..
zip -r /tmp/get-account-details.zip data/

aws lambda create-function \
  --function-name get-account-details \
  --runtime python3.12 \
  --role arn:aws:iam::${AWS_ACCOUNT_ID}:role/banking-lambda-tools-role \
  --handler handler.handler \
  --zip-file fileb:///tmp/get-account-details.zip \
  --timeout 30 \
  --memory-size 256 \
  --region $AWS_REGION
```

#### Step 3 — Deploy AgentCore Runtime

```bash
# Configure (creates/updates .bedrock_agentcore.yaml)
agentcore configure \
  --entrypoint agentcore_langgraph_runtime.py \
  --non-interactive

# Deploy (builds Docker image, pushes to ECR, creates runtime)
# First run takes ~5-10 minutes
agentcore launch

# Check status and get the ARN
agentcore status
```

Copy the `agentRuntimeArn` from the output. It looks like:
```
arn:aws:bedrock-agentcore:us-east-2:123456789012:runtime/banking_assistant-XXXXXXXX
```

#### Step 4 — Deploy API Gateway Invoker Lambda

```bash
AGENT_RUNTIME_ARN="arn:aws:bedrock-agentcore:us-east-2:ACCOUNT:runtime/banking_assistant-XXXXXXXX"

cd deploy/lambda/api_gateway_invoker
zip -r /tmp/api-gateway-invoker.zip .

aws lambda create-function \
  --function-name api-gateway-invoker \
  --runtime python3.12 \
  --role arn:aws:iam::${AWS_ACCOUNT_ID}:role/banking-lambda-invoker-role \
  --handler handler.handler \
  --zip-file fileb:///tmp/api-gateway-invoker.zip \
  --timeout 60 \
  --memory-size 256 \
  --environment "Variables={AGENT_RUNTIME_ARN=$AGENT_RUNTIME_ARN,AWS_REGION=$AWS_REGION}" \
  --region $AWS_REGION
```

#### Step 5 — Create API Gateway

```bash
# Create HTTP API
API_ID=$(aws apigatewayv2 create-api \
  --name banking-assistant-api \
  --protocol-type HTTP \
  --region $AWS_REGION \
  --query "ApiId" --output text)

echo "API ID: $API_ID"

# Create Lambda integration
INVOKER_ARN="arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:api-gateway-invoker"

INTEGRATION_ID=$(aws apigatewayv2 create-integration \
  --api-id $API_ID \
  --integration-type AWS_PROXY \
  --integration-uri $INVOKER_ARN \
  --payload-format-version "2.0" \
  --region $AWS_REGION \
  --query "IntegrationId" --output text)

# Create POST / route
aws apigatewayv2 create-route \
  --api-id $API_ID \
  --route-key "POST /" \
  --target "integrations/$INTEGRATION_ID" \
  --region $AWS_REGION

# Create $default stage with auto-deploy
aws apigatewayv2 create-stage \
  --api-id $API_ID \
  --stage-name '$default' \
  --auto-deploy \
  --region $AWS_REGION

# Grant API Gateway permission to invoke Lambda
aws lambda add-permission \
  --function-name api-gateway-invoker \
  --statement-id apigw-invoke \
  --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com \
  --source-arn "arn:aws:execute-api:${AWS_REGION}:${AWS_ACCOUNT_ID}:${API_ID}/*/*" \
  --region $AWS_REGION

echo "API Endpoint: https://${API_ID}.execute-api.${AWS_REGION}.amazonaws.com"
```

---

## Testing

### curl

```bash
API_URL="https://YOUR_API_ID.execute-api.us-east-2.amazonaws.com"

# Balance query
curl -X POST $API_URL/ \
  -H "Content-Type: application/json" \
  -d '{"prompt": "What is the balance for ACC001?"}'

# Details query
curl -X POST $API_URL/ \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Who is the owner of account ACC002?"}'

# Combined query
curl -X POST $API_URL/ \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Give me a full summary for ACC003 including balance and owner details"}'
```

### Postman

1. Open Postman
2. Click **Import** → select `deploy/postman/banking_assistant.postman_collection.json`
3. Click the collection → **Variables** tab
4. Set `base_url` to your API Gateway endpoint (e.g. `https://abc123.execute-api.us-east-2.amazonaws.com`)
5. Run any request

### agentcore CLI (direct runtime test, bypasses API Gateway)

```bash
agentcore invoke '{"prompt": "What is the balance for ACC001?"}'
```

---

## Updating After Code Changes

### Update tool Lambdas only

```bash
# Re-zip and update
cd deploy/lambda/get_account_balance
zip -r /tmp/get-account-balance.zip .
cd ../../..
zip -r /tmp/get-account-balance.zip data/

aws lambda update-function-code \
  --function-name get-account-balance \
  --zip-file fileb:///tmp/get-account-balance.zip \
  --region us-east-2
```

### Update AgentCore Runtime (agent logic change)

```bash
agentcore launch   # builds new container version, updates DEFAULT endpoint
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `ResourceNotFoundException` in invoker Lambda | Wrong `AGENT_RUNTIME_ARN` env var | Check `agentcore status`, update Lambda env var |
| `AccessDeniedException` | Missing IAM permissions | Verify `banking-lambda-invoker-role` has `bedrock-agentcore:InvokeAgentRuntime` |
| Lambda timeout | AgentCore cold start | Increase invoker Lambda timeout to 120s |
| Tool Lambda `FileNotFoundError` | `data/` not in zip | Re-package: zip from repo root to include `data/` |
| `ValidationException` on tool call | Tool name format issue | Ensure tool names are `snake_case` (already fixed in `banking_tools.py`) |
| API Gateway 502 | Lambda crash | Check CloudWatch Logs: `/aws/lambda/api-gateway-invoker` |

### View logs

```bash
# Invoker Lambda logs
aws logs tail /aws/lambda/api-gateway-invoker --follow --region us-east-2

# Tool Lambda logs
aws logs tail /aws/lambda/get-account-balance --follow --region us-east-2
aws logs tail /aws/lambda/get-account-details --follow --region us-east-2
```

---

## Tear Down

```bash
chmod +x deploy/destroy.sh
./deploy/destroy.sh
```
