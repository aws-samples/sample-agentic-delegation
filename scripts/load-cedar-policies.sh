#!/usr/bin/env bash
# Load Cedar policies into AgentCore Policy Engine.
#
# Prerequisites:
#   - AWS CLI configured
#   - bedrock-agentcore-starter-toolkit installed (pip install bedrock-agentcore-starter-toolkit)
#   - Gateway already deployed (run `cd infra && npx cdk deploy` first)
#
# Usage:
#   ./scripts/load-cedar-policies.sh [GATEWAY_ID] [REGION]

set -euo pipefail

GATEWAY_ID="${1:-}"
REGION="${2:-us-east-1}"
POLICY_DIR="$(cd "$(dirname "$0")/../infra/policies" && pwd)"

if [ -z "$GATEWAY_ID" ]; then
  echo "Usage: $0 <GATEWAY_ID> [REGION]"
  echo ""
  echo "Find your gateway ID from the CDK deploy output or:"
  echo "  aws bedrock-agentcore list-gateways --region $REGION"
  exit 1
fi

echo "Creating Policy Engine..."
POLICY_ENGINE_ID=$(aws bedrock-agentcore create-policy-engine \
  --region "$REGION" \
  --query 'policyEngineId' \
  --output text 2>/dev/null || echo "")

if [ -z "$POLICY_ENGINE_ID" ]; then
  echo "Failed to create policy engine. Check your IAM permissions."
  exit 1
fi

echo "Policy Engine created: $POLICY_ENGINE_ID"

echo "Loading Cedar schema..."
aws bedrock-agentcore put-policy-engine-schema \
  --region "$REGION" \
  --policy-engine-id "$POLICY_ENGINE_ID" \
  --schema "$(cat "$POLICY_DIR/delegation-schema.cedarschema")"

echo "Loading Cedar policies..."
POLICY_CONTENT=$(cat "$POLICY_DIR/delegation-policies.cedar")

aws bedrock-agentcore create-policy \
  --region "$REGION" \
  --policy-engine-id "$POLICY_ENGINE_ID" \
  --policy-type STATIC \
  --definition "{\"static\": {\"statement\": $(echo "$POLICY_CONTENT" | jq -Rs .)}}"

echo "Associating Policy Engine with Gateway..."
aws bedrock-agentcore update-gateway \
  --region "$REGION" \
  --gateway-id "$GATEWAY_ID" \
  --policy-engine-id "$POLICY_ENGINE_ID"

echo ""
echo "Done. Cedar policies are now enforcing delegation chain rules on gateway: $GATEWAY_ID"
echo "Policy Engine ID: $POLICY_ENGINE_ID"
