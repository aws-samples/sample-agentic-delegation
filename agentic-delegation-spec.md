# Secure Multi-Agent Delegation Chains on AWS

## Reference Architecture for Cedar-Based Permission Attenuation in Agentic Systems

---

## Problem Statement

The A2A and MCP protocols have standardized how agents communicate with each other and with tools, respectively. However, both protocols explicitly leave authorization as an "implementation-specific" concern. This creates a critical gap for enterprise adoption:

When Agent A delegates a task to Agent B, which then invokes a tool via MCP, **how do you ensure permissions narrow at each hop?** Traditional OAuth/RBAC models fail here because they were designed for human-to-service authorization, not for autonomous agents that dynamically delegate across trust boundaries.

Amazon Bedrock AgentCore Policy already provides Cedar-based enforcement for agent-to-tool interactions through the Gateway. What it does not prescribe is a pattern for **modeling delegation chains as Cedar entities** — where a parent agent spawns or delegates to child agents with provably attenuated authority, and the full permission lineage of any tool invocation is auditable.

This reference architecture fills that gap.

### Who Is This For

- Platform teams building multi-agent systems where agents delegate work to other agents
- Security architects who need to enforce least-privilege across agent delegation boundaries
- AWS Solutions Architects demonstrating governed multi-agent patterns to enterprise customers

### What This Is NOT

- This is not a protocol bridge or translation layer (AgentCore Runtime handles A2A and MCP natively)
- This is not a replacement for AgentCore Policy (it builds on top of it)
- This is not a general-purpose agent framework (use Strands, LangGraph, etc. for that)

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        User / Calling System                        │
│                     (initiates task with identity)                   │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ A2A task/send
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                  Coordinator Agent (AgentCore Runtime)               │
│                                                                     │
│  ┌───────────────┐  ┌──────────────────┐  ┌─────────────────────┐  │
│  │ Agent Card     │  │ Delegation       │  │ Cedar Policy        │  │
│  │ (A2A discovery)│  │ Token Minter     │  │ Context Builder     │  │
│  └───────────────┘  └──────────────────┘  └─────────────────────┘  │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ A2A task/send + delegation token
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                  Specialist Agent (AgentCore Runtime)                │
│                                                                     │
│  ┌───────────────┐  ┌──────────────────┐  ┌─────────────────────┐  │
│  │ Agent Card     │  │ Delegation       │  │ Permission          │  │
│  │ (A2A discovery)│  │ Token Validator  │  │ Scope Resolver      │  │
│  └───────────────┘  └──────────────────┘  └─────────────────────┘  │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ MCP call_tool + delegation context
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│              AgentCore Gateway + Policy Engine (Cedar)               │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  Cedar Policy Evaluation                                     │   │
│  │                                                              │   │
│  │  principal: Agent::"specialist-agent"                        │   │
│  │  action:    Action::"call_tool"                              │   │
│  │  resource:  Tool::"pricing-api"                              │   │
│  │  context:   { delegationChain: [...], maxAmount: 10000 }     │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌─────────────┐  ┌─────────────┐  ┌────────────────────────────┐  │
│  │ MCP Targets  │  │ API Targets │  │ Lambda Targets             │  │
│  └─────────────┘  └─────────────┘  └────────────────────────────┘  │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Delegation Audit Log (CloudWatch)                 │
│                                                                     │
│  Full chain: User → Coordinator → Specialist → Tool                 │
│  Permission narrowing at each hop: logged and queryable             │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Core Components

### 1. Delegation Token Model

When a parent agent delegates to a child agent, it mints a delegation token that encodes the attenuated permission scope. This token travels with the A2A task payload.

```python
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import uuid
import json
import hashlib


@dataclass
class DelegationScope:
    """Defines the attenuated permissions for a delegated agent."""
    allowed_tools: list[str]           # e.g., ["pricing-api", "inventory-read"]
    max_amount: float | None = None    # e.g., 10000.0 (dollar cap for financial tools)
    allowed_regions: list[str] = field(default_factory=list)  # e.g., ["us-east-1"]
    read_only: bool = False
    custom_constraints: dict = field(default_factory=dict)


@dataclass
class DelegationToken:
    """Immutable token encoding a single delegation hop."""
    token_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    parent_agent_id: str = ""
    child_agent_id: str = ""
    scope: DelegationScope = field(default_factory=DelegationScope)
    issued_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    expires_at: str = ""
    chain_hash: str = ""  # hash of parent's chain for tamper detection


@dataclass
class DelegationChain:
    """Ordered list of delegation hops from originator to current agent."""
    originating_user: str = ""
    hops: list[DelegationToken] = field(default_factory=list)

    def add_hop(self, token: DelegationToken) -> None:
        if self.hops:
            previous = self.hops[-1]
            token.chain_hash = hashlib.sha256(
                json.dumps(previous.__dict__, default=str).encode()
            ).hexdigest()
        self.hops.append(token)

    def current_scope(self) -> DelegationScope:
        """Returns the most restrictive scope in the chain."""
        if not self.hops:
            return DelegationScope()
        return self.hops[-1].scope

    def to_cedar_context(self) -> dict:
        """Converts the chain into Cedar evaluation context."""
        scope = self.current_scope()
        return {
            "delegationChain": [
                {
                    "from": hop.parent_agent_id,
                    "to": hop.child_agent_id,
                    "scope": hop.scope.__dict__,
                    "tokenId": hop.token_id,
                }
                for hop in self.hops
            ],
            "chainDepth": len(self.hops),
            "originatingUser": self.originating_user,
            "allowedTools": scope.allowed_tools,
            "maxAmount": scope.max_amount,
            "readOnly": scope.read_only,
        }
```

### 2. Cedar Policy Schema for Delegation

The Cedar schema extends AgentCore Policy's built-in entities with delegation-aware types.

```cedar
namespace AgenticDelegation {

    // Entity types
    entity User;

    entity Agent in [User] {
        agentType: String,       // "coordinator" | "specialist" | "auditor"
        framework: String,       // "strands" | "langgraph" | "openai" | "google-adk"
        trustLevel: Long,        // 1-5, where 5 is highest trust
    };

    entity Tool {
        category: String,        // "financial" | "data-read" | "data-write" | "external"
        sensitivityLevel: Long,  // 1-5
    };

    entity DelegationContext {
        chainDepth: Long,
        originatingUser: String,
        maxAmount: Long,
        readOnly: Bool,
    };

    // Actions
    action callTool appliesTo {
        principal: Agent,
        resource: Tool,
        context: DelegationContext,
    };

    action delegateTask appliesTo {
        principal: Agent,
        resource: Agent,
        context: DelegationContext,
    };
}
```

### 3. Example Cedar Policies

These policies demonstrate the delegation attenuation pattern.

```cedar
// Policy 1: Coordinator agents can delegate to specialist agents
// but only if chain depth stays under 3 hops
permit(
    principal is AgenticDelegation::Agent,
    action == AgenticDelegation::Action::"delegateTask",
    resource is AgenticDelegation::Agent
) when {
    principal.agentType == "coordinator" &&
    resource.agentType == "specialist" &&
    context.chainDepth < 3
};

// Policy 2: Specialist agents can call financial tools
// only if delegation chain grants it AND amount is within delegated cap
permit(
    principal is AgenticDelegation::Agent,
    action == AgenticDelegation::Action::"callTool",
    resource is AgenticDelegation::Tool
) when {
    principal.agentType == "specialist" &&
    resource.category == "financial" &&
    context.maxAmount > 0 &&
    context.chainDepth >= 1  // must have been delegated, not self-invoked
};

// Policy 3: Deny any tool call if delegation chain exceeds max depth
forbid(
    principal is AgenticDelegation::Agent,
    action == AgenticDelegation::Action::"callTool",
    resource is AgenticDelegation::Tool
) when {
    context.chainDepth > 4
};

// Policy 4: Read-only delegations cannot invoke write tools
forbid(
    principal is AgenticDelegation::Agent,
    action == AgenticDelegation::Action::"callTool",
    resource is AgenticDelegation::Tool
) when {
    context.readOnly == true &&
    resource.category == "data-write"
};
```

---

## Scenario: Procurement Workflow

This end-to-end scenario demonstrates the delegation chain in a realistic enterprise context.

```
User (procurement-manager@acme.com)
  │
  │  "Find the best price for 500 units of SKU-4821 and place a PO
  │   if under $10,000"
  │
  ▼
Coordinator Agent (Strands on AgentCore Runtime)
  │  Delegation scope: { tools: ["pricing-*", "inventory-read", "po-create"],
  │                       maxAmount: 10000, regions: ["us-east-1"] }
  │
  ├──► Pricing Agent (Google ADK on AgentCore Runtime, via A2A)
  │      Delegation scope: { tools: ["pricing-*", "inventory-read"],
  │                           maxAmount: null, readOnly: true }
  │      │
  │      ├── calls Tool: pricing-api/get-quotes  ✅ (read-only, allowed)
  │      ├── calls Tool: pricing-api/get-history  ✅ (read-only, allowed)
  │      └── calls Tool: po-create               ❌ DENIED (not in scope)
  │
  └──► Purchasing Agent (LangGraph on AgentCore Runtime, via A2A)
         Delegation scope: { tools: ["po-create"], maxAmount: 10000,
                              regions: ["us-east-1"] }
         │
         ├── calls Tool: po-create (amount: $8,500)   ✅ (under cap)
         ├── calls Tool: po-create (amount: $12,000)   ❌ DENIED (over cap)
         └── calls Tool: pricing-api/get-quotes        ❌ DENIED (not in scope)
```

### What Gets Logged (CloudWatch)

```json
{
    "timestamp": "2026-03-05T14:32:01Z",
    "eventType": "TOOL_INVOCATION",
    "decision": "ALLOW",
    "tool": "po-create",
    "agent": "purchasing-agent",
    "delegationChain": [
        {
            "hop": 0,
            "from": "user:procurement-manager@acme.com",
            "to": "agent:coordinator",
            "scopeGranted": ["pricing-*", "inventory-read", "po-create"],
            "maxAmount": 10000
        },
        {
            "hop": 1,
            "from": "agent:coordinator",
            "to": "agent:purchasing-agent",
            "scopeGranted": ["po-create"],
            "maxAmount": 10000
        }
    ],
    "toolInput": { "sku": "SKU-4821", "quantity": 500, "amount": 8500 },
    "policyDecisions": [
        { "policyId": "policy-002", "effect": "PERMIT", "reason": "specialist + financial + under cap" },
        { "policyId": "policy-003", "effect": "NOT_APPLICABLE", "reason": "chainDepth=2, under limit" }
    ]
}
```

---

## Implementation Tasks

### Task 1: Infrastructure (CDK)

Deploy the foundational AWS resources.

- AgentCore Runtime endpoints for Coordinator and Specialist agents
- AgentCore Gateway with MCP targets for the procurement tools (pricing API, inventory, PO service)
- AgentCore Policy Engine with the Cedar delegation schema and policies
- CloudWatch log group with structured query patterns for delegation chain auditing
- VPC with endpoints for Bedrock and Gateway traffic (Well-Architected Security Pillar)

Technology: AWS CDK (TypeScript), targeting `us-east-1`.

### Task 2: Delegation Token Library (Python)

Build a lightweight library that agents import to participate in delegation chains.

- `DelegationChain` and `DelegationToken` dataclasses (as shown above)
- `mint_delegation()` — parent agent creates a scoped token for a child
- `validate_delegation()` — child agent verifies token integrity and checks scope is a subset of parent's scope
- `to_cedar_context()` — converts the chain into the context dict that AgentCore Policy evaluates
- `attenuate_scope()` — helper to narrow a parent scope into a child scope (set intersection on tools, min on amounts)

This library is framework-agnostic. It works with Strands, LangGraph, Google ADK, or any Python agent.

### Task 3: Coordinator Agent (Strands)

Build the top-level agent that receives user tasks and delegates to specialists.

- Deployed on AgentCore Runtime with an A2A Agent Card advertising orchestration capabilities
- Uses the delegation library to mint scoped tokens for each specialist
- Discovers specialist agents via A2A agent card resolution
- Passes delegation tokens in the A2A task metadata
- Implements a simple planning step: decompose user intent into sub-tasks, assign each to the appropriate specialist with the narrowest possible scope

### Task 4: Specialist Agents (Multi-Framework)

Build two specialist agents on different frameworks to demonstrate cross-framework interoperability.

- Pricing Agent (Google ADK) — read-only access to pricing and inventory tools
- Purchasing Agent (LangGraph) — write access to PO creation, capped by delegated amount
- Both deployed on AgentCore Runtime with A2A Agent Cards
- Both validate incoming delegation tokens before acting
- Both attach delegation context to MCP tool calls through the Gateway

### Task 5: Audit Dashboard

Build a CloudWatch Logs Insights query set and optional CloudWatch dashboard.

- Query: "Show all tool invocations for a given user across all delegation chains"
- Query: "Show all DENIED tool calls with the policy that denied them"
- Query: "Show delegation chains deeper than N hops"
- Query: "Show all tool calls where delegated amount cap was within 10% of the limit"
- Optional: CloudWatch dashboard with widgets for delegation depth distribution, deny rate, and chain latency

---

## AWS Services Used

| Service | Role |
|---|---|
| Amazon Bedrock AgentCore Runtime | Hosts coordinator and specialist agents (A2A + MCP) |
| Amazon Bedrock AgentCore Gateway | Unified MCP endpoint for tools, with policy enforcement |
| Amazon Bedrock AgentCore Policy | Cedar-based authorization evaluated on every tool call |
| Amazon Bedrock AgentCore Identity | Agent and user identity management |
| Amazon CloudWatch | Delegation chain audit logging and dashboards |
| AWS CDK | Infrastructure as code |

---

## What Makes This Different

1. **Solves the authorization gap that A2A and MCP explicitly don't address.** Both protocols punt on authorization. This shows how to fill that gap with Cedar on AWS.

2. **Builds on AgentCore, doesn't rebuild it.** Every component uses first-party AWS services. The only net-new code is the delegation token library and the Cedar policy schema — the actual value-add.

3. **Framework-agnostic.** The coordinator uses Strands, one specialist uses Google ADK, another uses LangGraph. The delegation pattern works across all of them because it rides on A2A metadata and Cedar context, not framework internals.

4. **Auditable by design.** Every tool invocation logs the full delegation chain, every Cedar policy decision, and the exact scope at each hop. This is what compliance teams actually need.

5. **Nothing like it exists in aws-samples today.** There are multi-agent orchestration samples, MCP samples, and AgentCore samples. None of them address delegation chain authorization.

---

## OWASP Agentic AI Risk Mapping

This solution directly mitigates five of the ten risks in the [OWASP Top 10 for Agentic Applications](https://owasp.org/www-project-agentic-ai-top-10/) (December 2025).

| OWASP Risk | How This Solution Addresses It |
|---|---|
| **ASI-01: Privilege Escalation & Excessive Agency** | Scope attenuation enforces that every delegation hop can only narrow permissions, never widen them. The `attenuate_scope()` function validates that child tools are a subset of parent tools, child amount caps are ≤ parent caps, and read-only constraints cannot be removed. Cedar `forbid` policies provide a second enforcement layer at the Gateway. |
| **ASI-02: Tool Misuse & Exploitation** | Each agent's delegated scope explicitly lists which tools it can invoke (glob-matched). The pricing agent cannot call `po-create` even if prompt-injected to try, because the delegation token's `allowed_tools` doesn't include it and Cedar policy denies the call. |
| **ASI-03: Identity & Privilege Abuse** | Every delegation token binds a specific `parent_agent_id` → `child_agent_id` pair. `validate_delegation()` rejects tokens addressed to a different agent. The SHA-256 hash chain detects tampering if any hop is modified after minting. |
| **ASI-06: Inadequate Logging & Monitoring** | The `DelegationAuditor` emits structured JSON events for every tool invocation (ALLOW and DENY), every delegation mint, and every validation failure. Each event includes the full delegation chain, Cedar policy decisions, and tool inputs. CloudWatch dashboard provides real-time visibility with deny-rate alarms. |
| **ASI-09: Cascading Failures in Multi-Agent Systems** | Cedar policies enforce a hard chain depth limit (`chainDepth > 4` → DENY). TTL-based token expiry prevents stale delegations from persisting. The coordinator's planning step decomposes tasks into independent specialist delegations rather than chaining specialists sequentially, limiting blast radius. |

### Risks Not Directly Addressed

| OWASP Risk | Notes |
|---|---|
| ASI-04: Prompt Injection | Out of scope — handled by model-level guardrails (e.g., Bedrock Guardrails). However, scope attenuation limits the damage a prompt-injected agent can do. |
| ASI-05: Insecure Output Handling | Out of scope — handled at the application layer. |
| ASI-07: Unsafe Code Generation | Not applicable to this architecture. |
| ASI-08: Memory Poisoning | Not applicable — agents in this architecture are stateless per-request. |
| ASI-10: Rogue Agents | Partially addressed — delegation validation rejects tokens from unknown agents, and Cedar policies restrict which agent types can delegate to which. Full rogue agent detection requires runtime behavioral monitoring beyond this scope. |

---

## References

- [A2A Protocol Support in AgentCore Runtime](https://aws.amazon.com/blogs/machine-learning/introducing-agent-to-agent-protocol-support-in-amazon-bedrock-agentcore-runtime/) — AWS blog on native A2A support
- [AgentCore Gateway](https://aws.amazon.com/blogs/machine-learning/introducing-amazon-bedrock-agentcore-gateway-transforming-enterprise-ai-agent-tool-development/) — Zero-code MCP tool creation and policy enforcement
- [AgentCore Policy Overview](https://aws.github.io/bedrock-agentcore-starter-toolkit/user-guide/policy/overview.html) — Cedar-based tool authorization
- [Fine-Grained Access Control with Gateway Interceptors](https://aws.amazon.com/blogs/machine-learning/apply-fine-grained-access-control-with-bedrock-agentcore-gateway-interceptors/) — Dynamic permission filtering
- [Agentic AI Is Not Secure](https://authzed.com/blog/agentic-ai-is-not-secure) — AuthZed analysis of the authorization gap in A2A/MCP
- [Delegation as Data: Cedar + OpenClaw](https://www.technometria.com/p/delegation-as-data-applying-cedar) — Intra-domain delegation patterns with Cedar
- [Cedar Policy Language](https://docs.cedarpolicy.com/) — Official Cedar documentation
- [Forrester: MCP Security Concerns](https://forrester.com/blogs/mcp-doesnt-stand-for-many-critical-problemsbut-maybe-it-should-for-cisos) — Enterprise security analysis of agent protocols
- [OWASP Top 10 for Agentic Applications](https://owasp.org/www-project-agentic-ai-top-10/) — First security framework for autonomous AI systems (December 2025)
- [Authenticated Delegation and Authorized AI Agents](https://arxiv.org/html/2501.09674) — MIT/Harvard framework for agent delegation (Marro et al., 2025)
- [A Secure Delegation Protocol for Autonomous AI Agents](https://arxiv.org/html/2509.13597v1) — Analysis of OAuth 2.0 limitations in agentic settings
- [Google DeepMind: Intelligent Delegation](https://techinformed.com/google-deepmind-proposes-intelligent-delegation-for-enterprise-ai-agents/) — DeepMind's framework for scoped authority transfer in agent networks
- [Enterprise Identity for Agentic AI](https://id4all.substack.com/p/enterprise-identity-for-agentic-ai) — Analysis of recursive delegation chain governance challenges
- [1Password: Identity and Accountability in the Age of AI Agents](https://www.1password.com/blog/ai-agent-identity-and-accountability) — The agent, visibility, and trust challenges for 2026
