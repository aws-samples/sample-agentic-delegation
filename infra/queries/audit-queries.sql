-- CloudWatch Logs Insights queries for delegation chain auditing.
-- Use these in the CloudWatch console against /agentic-delegation/audit.
-- Copy-paste each query block (without the leading comment) into Logs Insights.

-- 1. All tool invocations for a given user across all delegation chains
fields @timestamp, tool, agent, decision, delegationChain.0.from as originator
| filter originatingUser = "procurement-manager@acme.com"
| sort @timestamp desc
| limit 100

-- 2. All DENIED tool calls with the policy that denied them
fields @timestamp, tool, agent, decision, policyDecisions
| filter decision = "DENY"
| sort @timestamp desc
| limit 50

-- 3. Delegation chains deeper than N hops
fields @timestamp, tool, agent, chainDepth
| filter chainDepth > 2
| sort chainDepth desc
| limit 50

-- 4. Tool calls where delegated amount cap was within 10% of the limit
fields @timestamp, tool, agent, toolInput.amount as requestedAmount, maxAmount
| filter requestedAmount > (maxAmount * 0.9)
| sort @timestamp desc
| limit 50

-- 5. Delegation chain latency (time from first hop to tool invocation)
stats avg(duration) as avgLatency, max(duration) as maxLatency, count(*) as invocations by tool
| sort invocations desc
