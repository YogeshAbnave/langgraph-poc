#!/usr/bin/env bash
# =============================================================================
# deploy.sh — Full deployment: Lambda tools + AgentCore Runtime + API Gateway
# =============================================================================
# Usage:
#   chmod +x deploy/deploy.sh
#   ./deploy/deploy.sh
#
# Prerequisites:
#   - AWS CLI configured (aws configure)
#   - Python 3.10+ with uv or pip
#   - bedrock-agentcore-starter-toolkit installed
#   - Sufficient IAM permissions (Lambda, IAM, API Gateway, Bedrock AgentCore)
# =============================================================================

set -euo pipefail

# ── Configuration — edit these before running ─────────────────────────────────
AWS_REGION="${AWS_REGION:-us-east-2}"
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# Lambda function names
FN_BALANCE="get-account-balance"
FN_DETAILS="get-account-details"
FN_INVOKER="api-gateway-invoker"

# IAM role names
ROLE_LAMBDA_TOOLS="banking-lambda-tools-role"
ROLE_LAMBDA_INVOKER="banking-lambda-invoker-role"
ROLE_AGENTCORE="banking-agentcore-runtime-role"

# AgentCore Runtime name
AGENTCORE_RUNTIME_NAME="banking_assistant"

# API Gateway name
APIGW_NAME="banking-assistant-api"

# Python runtime for Lambda
LAMBDA_RUNTIME="python3.12"

# Script directory (repo root)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "============================================================"
echo " Banking Assistant — AWS Deployment"
echo " Account : $AWS_ACCOUNT_ID"
echo " Region  : $AWS_REGION"
echo "============================================================"
echo ""

# =============================================================================
# STEP 1 — IAM Roles
# =============================================================================
echo ">>> STEP 1: Creating IAM roles..."

create_role_if_missing() {
  local role_name="$1"
  local trust_policy_file="$2"
  local permissions_file="$3"

  if aws iam get-role --role-name "$role_name" &>/dev/null; then
    echo "    Role $role_name already exists — skipping creation."
  else
    echo "    Creating role: $role_name"
    aws iam create-role \
      --role-name "$role_name" \
      --assume-role-policy-document "file://$trust_policy_file" \
      --output text --query "Role.RoleName" > /dev/null

    aws iam put-role-policy \
      --role-name "$role_name" \
      --policy-name "${role_name}-policy" \
      --policy-document "file://$permissions_file"

    echo "    Waiting for role to propagate..."
    sleep 10
  fi
}

create_role_if_missing \
  "$ROLE_LAMBDA_TOOLS" \
  "$SCRIPT_DIR/iam/lambda_tool_trust_policy.json" \
  "$SCRIPT_DIR/iam/lambda_tool_permissions.json"

create_role_if_missing \
  "$ROLE_LAMBDA_INVOKER" \
  "$SCRIPT_DIR/iam/lambda_tool_trust_policy.json" \
  "$SCRIPT_DIR/iam/lambda_invoker_permissions.json"

create_role_if_missing \
  "$ROLE_AGENTCORE" \
  "$SCRIPT_DIR/iam/agentcore_runtime_trust_policy.json" \
  "$SCRIPT_DIR/iam/agentcore_runtime_permissions.json"

ROLE_LAMBDA_TOOLS_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${ROLE_LAMBDA_TOOLS}"
ROLE_LAMBDA_INVOKER_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${ROLE_LAMBDA_INVOKER}"
ROLE_AGENTCORE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${ROLE_AGENTCORE}"

echo "    ✓ IAM roles ready."
echo ""

# =============================================================================
# STEP 2 — Package and deploy tool Lambda functions
# =============================================================================
echo ">>> STEP 2: Packaging and deploying tool Lambda functions..."

package_and_deploy_lambda() {
  local fn_name="$1"
  local handler_dir="$2"
  local handler_module="$3"   # e.g. "handler.handler"
  local role_arn="$4"
  local zip_file="/tmp/${fn_name}.zip"

  echo "    Packaging $fn_name..."

  # Build zip: handler + data/ files
  rm -f "$zip_file"
  cd "$handler_dir"
  zip -r "$zip_file" . -x "*.pyc" -x "__pycache__/*" > /dev/null

  # Include data/ at the root of the zip (renamed from knowledge_base/)
  cd "$REPO_ROOT"
  zip -r "$zip_file" data/ > /dev/null

  echo "    Deploying $fn_name..."
  if aws lambda get-function --function-name "$fn_name" --region "$AWS_REGION" &>/dev/null; then
    # Update existing function
    aws lambda update-function-code \
      --function-name "$fn_name" \
      --zip-file "fileb://$zip_file" \
      --region "$AWS_REGION" \
      --output text --query "FunctionName" > /dev/null
    echo "    Updated $fn_name."
  else
    # Create new function
    aws lambda create-function \
      --function-name "$fn_name" \
      --runtime "$LAMBDA_RUNTIME" \
      --role "$role_arn" \
      --handler "$handler_module" \
      --zip-file "fileb://$zip_file" \
      --timeout 30 \
      --memory-size 256 \
      --region "$AWS_REGION" \
      --output text --query "FunctionName" > /dev/null
    echo "    Created $fn_name."
  fi
}

package_and_deploy_lambda \
  "$FN_BALANCE" \
  "$SCRIPT_DIR/lambda/get_account_balance" \
  "handler.handler" \
  "$ROLE_LAMBDA_TOOLS_ARN"

package_and_deploy_lambda \
  "$FN_DETAILS" \
  "$SCRIPT_DIR/lambda/get_account_details" \
  "handler.handler" \
  "$ROLE_LAMBDA_TOOLS_ARN"

echo "    ✓ Tool Lambda functions deployed."
echo ""

# =============================================================================
# STEP 3 — Deploy AgentCore Runtime
# =============================================================================
echo ">>> STEP 3: Deploying AgentCore Runtime..."
echo "    This uses the agentcore CLI to configure and launch the runtime."
echo ""

cd "$REPO_ROOT"

# Configure the runtime (creates/updates .bedrock_agentcore.yaml)
echo "    Running: agentcore configure..."
agentcore configure \
  --entrypoint agentcore_langgraph_runtime.py \
  --non-interactive

# Set environment variables for the runtime so it uses real Lambda
export USE_LAMBDA=true
export LAMBDA_BALANCE_FN="$FN_BALANCE"
export LAMBDA_DETAILS_FN="$FN_DETAILS"
export AWS_REGION="$AWS_REGION"

# Launch (build container + push to ECR + create/update runtime)
echo "    Running: agentcore launch..."
echo "    (This may take 5-10 minutes on first run)"
agentcore launch

# Capture the runtime ARN from agentcore status
echo "    Fetching runtime ARN..."
AGENTCORE_STATUS=$(agentcore status --output json 2>/dev/null || echo "{}")
AGENT_RUNTIME_ARN=$(echo "$AGENTCORE_STATUS" | python3 -c "
import sys, json
data = json.load(sys.stdin)
# Try common keys from agentcore status output
arn = data.get('agentRuntimeArn') or data.get('arn') or data.get('agent_runtime_arn') or ''
print(arn)
" 2>/dev/null || echo "")

if [ -z "$AGENT_RUNTIME_ARN" ]; then
  echo ""
  echo "    ⚠️  Could not auto-detect runtime ARN from 'agentcore status'."
  echo "    Run: agentcore status"
  echo "    Copy the agentRuntimeArn value and set it below, then re-run from STEP 4."
  echo ""
  read -rp "    Paste your agentRuntimeArn here: " AGENT_RUNTIME_ARN
fi

echo "    ✓ AgentCore Runtime ARN: $AGENT_RUNTIME_ARN"
echo ""

# =============================================================================
# STEP 4 — Deploy API Gateway Invoker Lambda
# =============================================================================
echo ">>> STEP 4: Deploying API Gateway Invoker Lambda..."

INVOKER_ZIP="/tmp/${FN_INVOKER}.zip"
rm -f "$INVOKER_ZIP"
cd "$SCRIPT_DIR/lambda/api_gateway_invoker"
zip -r "$INVOKER_ZIP" . -x "*.pyc" -x "__pycache__/*" > /dev/null

if aws lambda get-function --function-name "$FN_INVOKER" --region "$AWS_REGION" &>/dev/null; then
  aws lambda update-function-code \
    --function-name "$FN_INVOKER" \
    --zip-file "fileb://$INVOKER_ZIP" \
    --region "$AWS_REGION" \
    --output text --query "FunctionName" > /dev/null

  aws lambda update-function-configuration \
    --function-name "$FN_INVOKER" \
    --environment "Variables={AGENT_RUNTIME_ARN=$AGENT_RUNTIME_ARN,AWS_REGION=$AWS_REGION}" \
    --region "$AWS_REGION" \
    --output text --query "FunctionName" > /dev/null
  echo "    Updated $FN_INVOKER."
else
  aws lambda create-function \
    --function-name "$FN_INVOKER" \
    --runtime "$LAMBDA_RUNTIME" \
    --role "$ROLE_LAMBDA_INVOKER_ARN" \
    --handler "handler.handler" \
    --zip-file "fileb://$INVOKER_ZIP" \
    --timeout 60 \
    --memory-size 256 \
    --environment "Variables={AGENT_RUNTIME_ARN=$AGENT_RUNTIME_ARN,AWS_REGION=$AWS_REGION}" \
    --region "$AWS_REGION" \
    --output text --query "FunctionName" > /dev/null
  echo "    Created $FN_INVOKER."
fi

echo "    ✓ Invoker Lambda deployed."
echo ""

# =============================================================================
# STEP 5 — Create API Gateway (HTTP API)
# =============================================================================
echo ">>> STEP 5: Creating API Gateway (HTTP API)..."

# Check if API already exists
EXISTING_API_ID=$(aws apigatewayv2 get-apis \
  --region "$AWS_REGION" \
  --query "Items[?Name=='$APIGW_NAME'].ApiId" \
  --output text 2>/dev/null || echo "")

if [ -n "$EXISTING_API_ID" ] && [ "$EXISTING_API_ID" != "None" ]; then
  API_ID="$EXISTING_API_ID"
  echo "    API Gateway '$APIGW_NAME' already exists (ID: $API_ID) — reusing."
else
  API_ID=$(aws apigatewayv2 create-api \
    --name "$APIGW_NAME" \
    --protocol-type HTTP \
    --region "$AWS_REGION" \
    --query "ApiId" \
    --output text)
  echo "    Created API Gateway: $API_ID"
fi

# Lambda ARN for the invoker
INVOKER_LAMBDA_ARN="arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:${FN_INVOKER}"

# Create or reuse Lambda integration
INTEGRATION_ID=$(aws apigatewayv2 get-integrations \
  --api-id "$API_ID" \
  --region "$AWS_REGION" \
  --query "Items[?IntegrationUri=='$INVOKER_LAMBDA_ARN'].IntegrationId" \
  --output text 2>/dev/null || echo "")

if [ -z "$INTEGRATION_ID" ] || [ "$INTEGRATION_ID" = "None" ]; then
  INTEGRATION_ID=$(aws apigatewayv2 create-integration \
    --api-id "$API_ID" \
    --integration-type AWS_PROXY \
    --integration-uri "$INVOKER_LAMBDA_ARN" \
    --payload-format-version "2.0" \
    --region "$AWS_REGION" \
    --query "IntegrationId" \
    --output text)
  echo "    Created Lambda integration: $INTEGRATION_ID"
fi

# Create POST / route
ROUTE_KEY="POST /"
EXISTING_ROUTE=$(aws apigatewayv2 get-routes \
  --api-id "$API_ID" \
  --region "$AWS_REGION" \
  --query "Items[?RouteKey=='$ROUTE_KEY'].RouteId" \
  --output text 2>/dev/null || echo "")

if [ -z "$EXISTING_ROUTE" ] || [ "$EXISTING_ROUTE" = "None" ]; then
  aws apigatewayv2 create-route \
    --api-id "$API_ID" \
    --route-key "$ROUTE_KEY" \
    --target "integrations/$INTEGRATION_ID" \
    --region "$AWS_REGION" \
    --output text --query "RouteId" > /dev/null
  echo "    Created route: POST /"
fi

# Create $default stage with auto-deploy
EXISTING_STAGE=$(aws apigatewayv2 get-stages \
  --api-id "$API_ID" \
  --region "$AWS_REGION" \
  --query "Items[?StageName=='\$default'].StageName" \
  --output text 2>/dev/null || echo "")

if [ -z "$EXISTING_STAGE" ] || [ "$EXISTING_STAGE" = "None" ]; then
  aws apigatewayv2 create-stage \
    --api-id "$API_ID" \
    --stage-name '$default' \
    --auto-deploy \
    --region "$AWS_REGION" \
    --output text --query "StageName" > /dev/null
  echo "    Created \$default stage with auto-deploy."
fi

# Grant API Gateway permission to invoke the Lambda
aws lambda add-permission \
  --function-name "$FN_INVOKER" \
  --statement-id "apigw-invoke-$(date +%s)" \
  --action "lambda:InvokeFunction" \
  --principal "apigateway.amazonaws.com" \
  --source-arn "arn:aws:execute-api:${AWS_REGION}:${AWS_ACCOUNT_ID}:${API_ID}/*/*" \
  --region "$AWS_REGION" \
  --output text --query "Statement" > /dev/null 2>&1 || true

API_ENDPOINT="https://${API_ID}.execute-api.${AWS_REGION}.amazonaws.com"
echo "    ✓ API Gateway endpoint: $API_ENDPOINT"
echo ""

# =============================================================================
# STEP 6 — Save deployment outputs
# =============================================================================
echo ">>> STEP 6: Saving deployment outputs..."

cat > "$REPO_ROOT/deploy/outputs.env" << EOF
# Generated by deploy.sh — $(date)
AWS_ACCOUNT_ID=$AWS_ACCOUNT_ID
AWS_REGION=$AWS_REGION
AGENT_RUNTIME_ARN=$AGENT_RUNTIME_ARN
API_GATEWAY_ID=$API_ID
API_GATEWAY_ENDPOINT=$API_ENDPOINT
LAMBDA_BALANCE_FN=$FN_BALANCE
LAMBDA_DETAILS_FN=$FN_DETAILS
LAMBDA_INVOKER_FN=$FN_INVOKER
ROLE_LAMBDA_TOOLS_ARN=$ROLE_LAMBDA_TOOLS_ARN
ROLE_LAMBDA_INVOKER_ARN=$ROLE_LAMBDA_INVOKER_ARN
ROLE_AGENTCORE_ARN=$ROLE_AGENTCORE_ARN
EOF

echo "    Outputs saved to deploy/outputs.env"
echo ""

# =============================================================================
# Summary
# =============================================================================
echo "============================================================"
echo " ✅  DEPLOYMENT COMPLETE"
echo "============================================================"
echo ""
echo "  API Gateway URL : $API_ENDPOINT"
echo "  AgentCore ARN   : $AGENT_RUNTIME_ARN"
echo ""
echo "  Test with curl:"
echo "    curl -X POST $API_ENDPOINT \\"
echo "      -H 'Content-Type: application/json' \\"
echo "      -d '{\"prompt\": \"What is the balance for ACC001?\"}'"
echo ""
echo "  Import Postman collection:"
echo "    deploy/postman/banking_assistant.postman_collection.json"
echo "    Set variable: base_url = $API_ENDPOINT"
echo ""
echo "  To destroy all resources:"
echo "    ./deploy/destroy.sh"
echo "============================================================"
