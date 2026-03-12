# Secure Multi-Agent Delegation Chains on AWS

> Reference architecture for Cedar-based permission attenuation in agentic systems using Amazon Bedrock AgentCore.

## Important

This project is a reference architecture for demonstration and educational purposes only. It is not intended for production use. The Lambda functions use mock data, the agents require additional configuration for real model access, and the code has not undergone a formal AWS security review. Use it to learn the delegation chain pattern and adapt the concepts to your own production systems with appropriate security review, testing, and hardening.

## The Problem

The [A2A](https://google.github.io/A2A/) and [MCP](https://modelcontextprotocol.io/) protocols standardize how agents communicate with each other and with tools. Both explicitly leave authorization as "implementation-specific." This creates a critical gap:

**When Agent A delegates a task to Agent B, which then invokes a tool via MCP, how do you ensure permissions narrow at each hop?**

This project fills that gap with a delegation token library, Cedar policy schema, and three working agents that demonstrate the pattern end-to-end on Amazon Bedrock AgentCore.

## Architecture

```text
User (procurement-manager@acme.com)
  │
  ▼
Coordinator Agent (Strands on AgentCore Runtime)
  │
  ├──► Pricing Agent (Google ADK, via A2A)     ── read-only scope
  │      └── pricing-api, inventory-read        ✅ allowed
  │      └── po-create                          ❌ denied (not in scope)
  │
  └──► Purchasing Agent (LangGraph, via A2A)   ── amount-capped scope
         └── po-create ($8,500)                 ✅ allowed (under $10k cap)
         └── po-create ($12,000)                ❌ denied (over cap)
         └── pricing-api                        ❌ denied (not in scope)
  │
  ▼
AgentCore Gateway + Cedar Policy Engine
  │
  ▼
CloudWatch Audit Log (full chain at every decision)
```

Each delegation hop mints a scoped token that narrows permissions. Cedar policies enforce these constraints at the Gateway. Every decision is logged with the full delegation chain for audit.

## Quick Start

### 1. Run the demo (no AWS account needed)

```bash
git clone https://github.com/aws-samples/sample-agentic-delegation.git
cd sample-agentic-delegation
pip install pytest
python3 scripts/demo.py
```

This runs the full procurement scenario locally, showing delegation token minting, scope enforcement, tamper detection, and structured audit output at each step.

### 2. Run the tests

```bash
python3 -m pytest tests/ -v
```

48 tests covering the delegation library: scope attenuation, chain integrity, token validation, audit logging, and the full end-to-end procurement workflow.

### 3. Deploy to AWS

```bash
cd infra
npm install
npx cdk bootstrap   # first time only
npx cdk deploy
```

After deployment, load the Cedar policies into the AgentCore Policy Engine:

```bash
# Get the gateway ID from CDK output
./scripts/load-cedar-policies.sh <GATEWAY_ID> us-east-1
```

### 4. Local multi-agent development (Docker)

```bash
docker compose up --build
```

Runs all three agents locally: coordinator on `:9000`, pricing on `:9001`, purchasing on `:9002`.

## Project Structure

```text
├── lib/delegation/              # Delegation token library (Python, framework-agnostic)
│   ├── models.py                #   DelegationScope, DelegationToken, DelegationChain
│   ├── attenuate.py             #   Scope narrowing with subset enforcement
│   ├── mint.py                  #   Token minting with attenuation validation
│   ├── validate.py              #   Receiving-side chain validation (integrity, identity, expiry)
│   └── audit.py                 #   Structured JSON audit logging for CloudWatch
├── agents/
│   ├── coordinator/             # Strands agent — task decomposition & delegation
│   ├── pricing/                 # Google ADK agent — read-only price research
│   └── purchasing/              # LangGraph agent — PO creation with amount caps
├── infra/                       # AWS CDK (TypeScript)
│   ├── lib/
│   │   ├── delegation-mesh-stack.ts       # AgentCore Runtime, Gateway, Lambda tools
│   │   └── delegation-audit-dashboard.ts  # CloudWatch dashboard + alarms
│   ├── policies/
│   │   ├── delegation-policies.cedar      # Cedar authorization policies
│   │   └── delegation-schema.cedarschema  # Cedar entity schema
│   └── queries/
│       └── audit-queries.sql              # CloudWatch Logs Insights queries
├── scripts/
│   ├── demo.py                  # Local demo — full scenario without AWS
│   └── load-cedar-policies.sh   # Load Cedar policies into AgentCore Policy Engine
├── tests/
│   ├── test_delegation.py       # Unit tests for the delegation library
│   ├── test_integration.py      # End-to-end procurement workflow tests
│   └── test_audit.py            # Audit logging tests
└── docker-compose.yml           # Local multi-agent development
```

## How Delegation Works

1. User sends a task to the **Coordinator** agent
2. Coordinator mints a **delegation token** with a narrowed scope for each specialist
3. Token travels with the A2A task metadata to the specialist agent
4. Specialist **validates** the token — checks integrity (hash chain), identity, and expiry
5. Specialist checks tool permissions and amount caps against the delegated scope
6. Specialist attaches the **Cedar context** to MCP tool calls through the Gateway
7. AgentCore Policy Engine evaluates Cedar policies against the delegation context
8. Every decision (ALLOW/DENY) is **audit logged** with the full chain

### Scope Attenuation Rules

Permissions can only narrow, never widen:

- Child tools must match parent tool patterns (glob matching: `pricing-*` permits `pricing-api`)
- Child amount cap must be ≤ parent cap (cannot remove a cap the parent set)
- Child regions must be a subset of parent regions
- `read_only` cannot be de-escalated (once set, it sticks)
- Custom constraints must be inherited (child can add, not remove)

### Tamper Detection

Each delegation token includes a `chain_hash` — a SHA-256 of the previous hop's serialized content. If any hop in the chain is modified after minting, `verify_integrity()` detects it and `validate_delegation()` rejects the chain.

## Cedar Policies

The Cedar schema extends AgentCore Policy with delegation-aware types:

```cedar
namespace AgenticDelegation {
    entity Agent in [User] {
        agentType: String,    // "coordinator" | "specialist"
        trustLevel: Long,
    };
    entity Tool {
        category: String,     // "financial" | "data-read" | "data-write"
    };
    action callTool appliesTo {
        principal: Agent,
        resource: Tool,
        context: DelegationContext,
    };
}
```

Key policies:
- Coordinators can delegate to specialists if chain depth < 3
- Specialists can call financial tools only with a valid delegation and amount cap
- Chain depth > 4 is forbidden (hard limit)
- Read-only delegations cannot invoke write tools

See [`infra/policies/`](infra/policies/) for the full schema and policy set.

## Audit Dashboard

The CDK stack deploys a CloudWatch dashboard with:
- Tool invocation decisions (ALLOW vs DENY over time)
- Delegation chain depth distribution
- Per-agent and per-tool activity breakdown
- Near-cap warnings (amount > 90% of limit)
- Deep chain alerts (depth > 2)
- High deny rate alarm (>10 denials in 5 minutes)

Pre-built Logs Insights queries are in [`infra/queries/audit-queries.sql`](infra/queries/audit-queries.sql).

## AWS Services Used

| Service | Role |
| --- | --- |
| Amazon Bedrock AgentCore Runtime | Hosts all three agents with A2A + MCP support |
| Amazon Bedrock AgentCore Gateway | Unified MCP tool endpoint with Cedar policy enforcement |
| Amazon Bedrock AgentCore Policy | Cedar-based authorization evaluated on every tool call |
| Amazon CloudWatch | Delegation chain audit logging, dashboard, and alarms |
| AWS Lambda | Backend for procurement tools (pricing, inventory, PO creation) |
| AWS CDK | Infrastructure as code |

## OWASP Agentic AI Risk Coverage

This solution directly mitigates five of the [OWASP Top 10 for Agentic Applications](https://owasp.org/www-project-agentic-ai-top-10/) (December 2025):

| OWASP Risk | Mitigation |
| --- | --- |
| ASI-01: Privilege Escalation | Scope attenuation — every hop can only narrow, never widen permissions |
| ASI-02: Tool Misuse | Delegated tool allowlists with glob matching + Cedar enforcement at Gateway |
| ASI-03: Identity & Privilege Abuse | Token binding (parent→child), SHA-256 hash chain tamper detection |
| ASI-06: Inadequate Logging | Structured audit events for every ALLOW/DENY with full chain context |
| ASI-09: Cascading Failures | Chain depth limits, TTL-based token expiry, independent specialist delegation |

See the [full mapping](agentic-delegation-spec.md#owasp-agentic-ai-risk-mapping) in the spec for details on each risk and what's not covered.

## Prerequisites

- Python 3.12+
- Node.js 18+ and npm (for CDK)
- AWS CLI configured with credentials (for deployment)
- AWS CDK CLI: `npm install -g aws-cdk`
- Docker (optional, for local multi-agent development)

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
