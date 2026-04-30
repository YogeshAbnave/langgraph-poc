#!/usr/bin/env bash
# =============================================================================
# destroy.sh — Tear down all deployed resources
# =============================================================================
# Usage:
#   chmod +x deploy/destroy.sh
#   ./deploy/destroy.sh
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load outputs if available
if [ -f "$SCRIPT_DIR/outputs.env" ]; then
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/outputs.env"
  echo "Loaded deployment outputs from deploy/outputs.env"
else
  echo "⚠️  deploy/outputs.env not found. Using environment variables or defaults."
fi

AWS_REGION="${AWS_REGION:-us-east-2}"
APIGW_NAME="${APIGW_NAME:-banking-assistant-api}"

echo "============================================================"
echo " Banking Assistant — Destroy Resources"
echo " Region: $AWS_REGION"
echo "============================================================"
echo ""
read -rp "Are you sure you want to destroy all resources? (yes/no): " CONFIRM
if [ "$CONFIRM" != "yes" ]; then
  echo "Aborted."
  exit 0
fi

# ── AgentCore Runtime ─────────────────────────────────────────────────────────
echo ">>> Destroying AgentCore Runtime..."
cd "$REPO_ROOT"
agentcore destroy --yes 2>/dev/null || echo "    agentcore destroy failed or already destroyed."

# ── Lambda functions ──────────────────────────────────────────────────────────
echo ">>> Deleting Lambda functions..."
for fn in "get-account-balance" "get-account-details" "api-gateway-invoker"; do
  aws lambda delete-function --function-name "$fn" --region "$AWS_REGION" 2>/dev/null \
    && echo "    Deleted $fn" \
    || echo "    $fn not found — skipping."
done

# ── API Gateway ───────────────────────────────────────────────────────────────
echo ">>> Deleting API Gateway..."
API_ID=$(aws apigatewayv2 get-apis \
  --region "$AWS_REGION" \
  --query "Items[?Name=='$APIGW_NAME'].ApiId" \
  --output text 2>/dev/null || echo "")

if [ -n "$API_ID" ] && [ "$API_ID" != "None" ]; then
  aws apigatewayv2 delete-api --api-id "$API_ID" --region "$AWS_REGION"
  echo "    Deleted API Gateway: $API_ID"
else
  echo "    API Gateway not found — skipping."
fi

# ── IAM Roles ─────────────────────────────────────────────────────────────────
echo ">>> Deleting IAM roles..."
for role in "banking-lambda-tools-role" "banking-lambda-invoker-role" "banking-agentcore-runtime-role"; do
  # Delete inline policies first
  POLICIES=$(aws iam list-role-policies --role-name "$role" --query "PolicyNames" --output text 2>/dev/null || echo "")
  for policy in $POLICIES; do
    aws iam delete-role-policy --role-name "$role" --policy-name "$policy" 2>/dev/null || true
  done
  aws iam delete-role --role-name "$role" 2>/dev/null \
    && echo "    Deleted role: $role" \
    || echo "    Role $role not found — skipping."
done

echo ""
echo "============================================================"
echo " ✅  All resources destroyed."
echo "============================================================"
