"""Coordinator Agent — Strands-based A2A server on AgentCore Runtime.

Decomposes procurement tasks and delegates to specialist agents
with attenuated delegation scopes via Cedar-aware tokens.
"""

import json
import logging
import os

from strands import Agent, tool
from strands.models.bedrock import BedrockModel
from bedrock_agentcore.runtime import BedrockAgentCoreApp

# Import delegation library (mounted at /opt/lib in container)
import sys
sys.path.insert(0, "/opt")
from lib.delegation.models import (
    DelegationChain,
    DelegationScope,
)
from lib.delegation.mint import mint_delegation
from lib.delegation.audit import DelegationAuditor

logger = logging.getLogger(__name__)
auditor = DelegationAuditor("coordinator")

PRICING_AGENT_URL = os.environ.get(
    "PRICING_AGENT_URL", ""
)
PURCHASING_AGENT_URL = os.environ.get(
    "PURCHASING_AGENT_URL", ""
)
GATEWAY_URL = os.environ.get("GATEWAY_URL", "")


def build_initial_chain(user_id: str) -> DelegationChain:
    """Build the root delegation chain from the originating user."""
    chain = DelegationChain(originating_user=user_id)
    root_scope = DelegationScope(
        allowed_tools=(
            "pricing-api*",
            "inventory-read*",
            "po-create*",
        ),
        max_amount=10000.0,
        allowed_regions=("us-east-1",),
    )
    mint_delegation(
        chain,
        parent_agent_id="user",
        child_agent_id="coordinator",
        child_scope=root_scope,
        ttl_seconds=3600,
    )
    auditor.log_delegation_mint(chain, "user", "coordinator")
    return chain


def mint_pricing_delegation(
    chain: DelegationChain,
) -> tuple[DelegationChain, dict]:
    """Mint a read-only delegation for the pricing agent."""
    import copy
    pricing_chain = copy.deepcopy(chain)
    pricing_scope = DelegationScope(
        allowed_tools=("pricing-api*", "inventory-read*"),
        read_only=True,
        allowed_regions=("us-east-1",),
    )
    pricing_chain, token = mint_delegation(
        pricing_chain,
        parent_agent_id="coordinator",
        child_agent_id="pricing-agent",
        child_scope=pricing_scope,
        ttl_seconds=1800,
    )
    return pricing_chain, pricing_chain.to_cedar_context()


def mint_purchasing_delegation(
    chain: DelegationChain,
    max_amount: float,
) -> tuple[DelegationChain, dict]:
    """Mint a write-capable delegation for the purchasing agent."""
    import copy
    purchasing_chain = copy.deepcopy(chain)
    purchasing_scope = DelegationScope(
        allowed_tools=("po-create*",),
        max_amount=max_amount,
        allowed_regions=("us-east-1",),
    )
    purchasing_chain, token = mint_delegation(
        purchasing_chain,
        parent_agent_id="coordinator",
        child_agent_id="purchasing-agent",
        child_scope=purchasing_scope,
        ttl_seconds=1800,
    )
    return purchasing_chain, purchasing_chain.to_cedar_context()


@tool
def delegate_to_pricing(
    sku: str, quantity: int, user_id: str
) -> dict:
    """Delegate pricing research to the pricing specialist agent.

    Args:
        sku: Product SKU to research.
        quantity: Desired order quantity.
        user_id: Originating user identity.
    """
    chain = build_initial_chain(user_id)
    pricing_chain, cedar_ctx = mint_pricing_delegation(chain)

    # In production, this would be an A2A task/send call
    # to the pricing agent's AgentCore Runtime endpoint.
    # For the reference architecture, we return the delegation
    # metadata showing what WOULD be sent.
    return {
        "delegated_to": "pricing-agent",
        "a2a_endpoint": PRICING_AGENT_URL,
        "task": {
            "action": "get_best_price",
            "sku": sku,
            "quantity": quantity,
        },
        "delegation_context": cedar_ctx,
        "chain_depth": pricing_chain.depth,
        "scope": {
            "tools": list(
                pricing_chain.current_scope().allowed_tools
            ),
            "read_only": True,
        },
    }


@tool
def delegate_to_purchasing(
    sku: str,
    quantity: int,
    amount: float,
    user_id: str,
) -> dict:
    """Delegate PO creation to the purchasing specialist agent.

    Args:
        sku: Product SKU to order.
        quantity: Order quantity.
        amount: Total PO amount in USD.
        user_id: Originating user identity.
    """
    chain = build_initial_chain(user_id)
    purchasing_chain, cedar_ctx = mint_purchasing_delegation(
        chain, max_amount=10000.0
    )

    # Check if amount exceeds delegated cap
    scope = purchasing_chain.current_scope()
    if not scope.amount_permitted(amount):
        return {
            "error": "DENIED",
            "reason": (
                f"Amount ${amount} exceeds delegated cap "
                f"${scope.max_amount}"
            ),
            "delegation_context": cedar_ctx,
        }

    return {
        "delegated_to": "purchasing-agent",
        "a2a_endpoint": PURCHASING_AGENT_URL,
        "task": {
            "action": "create_po",
            "sku": sku,
            "quantity": quantity,
            "amount": amount,
        },
        "delegation_context": cedar_ctx,
        "chain_depth": purchasing_chain.depth,
        "scope": {
            "tools": list(
                purchasing_chain.current_scope().allowed_tools
            ),
            "max_amount": scope.max_amount,
        },
    }


# ── Strands Agent ──────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a procurement coordinator agent.

Your job is to help users find the best prices for products and
place purchase orders when authorized.

You have two specialist agents you can delegate to:
1. Pricing Agent — for price research (read-only)
2. Purchasing Agent — for creating purchase orders (write, amount-capped)

Always delegate pricing research first, then use the results to
decide whether to delegate a purchase order.

Every delegation carries a Cedar-enforced scope that narrows
permissions at each hop. You cannot exceed your delegated authority.

When delegating, always pass the user_id from the original request.
"""

model = BedrockModel(
    model_id="us.anthropic.claude-sonnet-4-20250514",
    region_name=os.environ.get("AWS_REGION", "us-east-1"),
)

agent = Agent(
    model=model,
    system_prompt=SYSTEM_PROMPT,
    tools=[delegate_to_pricing, delegate_to_purchasing],
)

# ── AgentCore Runtime entrypoint ───────────────────────────────────

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: dict) -> dict:
    """Handle incoming requests from AgentCore Runtime."""
    prompt = payload.get("prompt", "")
    user_id = payload.get(
        "user_id", "anonymous@example.com"
    )

    # Inject user_id into the prompt context
    full_prompt = (
        f"[User: {user_id}]\n\n{prompt}"
    )

    result = agent(full_prompt)
    return {
        "response": str(result.message),
        "agent": "coordinator",
        "framework": "strands",
    }


if __name__ == "__main__":
    app.run()
