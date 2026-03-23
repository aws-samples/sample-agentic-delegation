"""Pricing Agent — Google ADK-based A2A server on AgentCore Runtime.

Read-only specialist that researches pricing and inventory data.
Validates incoming delegation tokens before acting.
"""

import json
import logging
import os
import sys

from google.adk import Agent as ADKAgent
from google.adk.tools import FunctionTool
from a2a.server.apps.starlette import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.agent_execution import AgentExecutor
from a2a.types import AgentCard, AgentSkill, AgentCapabilities
import uvicorn

# Import delegation library
sys.path.insert(0, "/opt")
from lib.delegation.models import DelegationChain, DelegationToken, DelegationScope
from lib.delegation.validate import validate_delegation, ValidationError
from lib.delegation.audit import DelegationAuditor

logger = logging.getLogger(__name__)
auditor = DelegationAuditor("pricing-agent")

GATEWAY_URL = os.environ.get("GATEWAY_URL", "")


# ── Delegation validation ──────────────────────────────────────────

def validate_incoming_delegation(
    delegation_context: dict,
) -> DelegationChain:
    """Reconstruct and validate a delegation chain from A2A metadata."""
    chain = DelegationChain(
        originating_user=delegation_context.get(
            "originatingUser", ""
        )
    )
    for hop_data in delegation_context.get(
        "delegationChain", []
    ):
        scope_data = hop_data.get("scope", {})
        scope = DelegationScope(
            allowed_tools=tuple(
                scope_data.get("allowed_tools", [])
            ),
            max_amount=scope_data.get("max_amount"),
            allowed_regions=tuple(
                scope_data.get("allowed_regions", [])
            ),
            read_only=scope_data.get("read_only", False),
        )
        token = DelegationToken(
            token_id=hop_data.get("tokenId", ""),
            parent_agent_id=hop_data.get("from", ""),
            child_agent_id=hop_data.get("to", ""),
            scope=scope,
        )
        chain.add_hop(token)

    validate_delegation(chain, "pricing-agent")
    return chain


# ── Mock tool implementations ──────────────────────────────────────

MOCK_PRICES = {
    "SKU-4821": {
        "unit_price": 17.00,
        "bulk_discount": 0.15,
        "supplier": "Acme Corp",
    },
    "SKU-1234": {
        "unit_price": 42.50,
        "bulk_discount": 0.10,
        "supplier": "GlobalParts",
    },
}


def get_quotes(sku: str, quantity: int) -> dict:
    """Get price quotes for a SKU with quantity-based discounts.

    Args:
        sku: Product SKU identifier.
        quantity: Desired order quantity.
    """
    price_info = MOCK_PRICES.get(
        sku,
        {
            "unit_price": 0,
            "bulk_discount": 0,
            "supplier": "Unknown",
        },
    )
    unit = (
        price_info["unit_price"]
        * (1 - price_info["bulk_discount"])
        if quantity >= 100
        else price_info["unit_price"]
    )
    return {
        "sku": sku,
        "quantity": quantity,
        "unit_price": round(unit, 2),
        "total": round(unit * quantity, 2),
        "supplier": price_info["supplier"],
    }


def get_price_history(sku: str) -> dict:
    """Get historical pricing data for a SKU.

    Args:
        sku: Product SKU identifier.
    """
    return {
        "sku": sku,
        "price_history": [
            {"date": "2026-01-15", "price": 18.50},
            {"date": "2026-02-15", "price": 17.00},
        ],
    }


def check_inventory(sku: str) -> dict:
    """Check available inventory for a SKU.

    Args:
        sku: Product SKU identifier.
    """
    inventory = {
        "SKU-4821": {
            "available": 2500,
            "warehouse": "us-east-1",
        },
        "SKU-1234": {
            "available": 150,
            "warehouse": "eu-west-1",
        },
    }
    inv = inventory.get(
        sku, {"available": 0, "warehouse": "unknown"}
    )
    return {"sku": sku, **inv}


# ── Google ADK Agent ───────────────────────────────────────────────

pricing_agent = ADKAgent(
    name="pricing_agent",
    model="gemini-2.0-flash",
    description="Pricing specialist — researches prices and inventory (read-only)",
    instruction="""You are a pricing research specialist.
You can look up price quotes, price history, and inventory levels.
You are READ-ONLY — you cannot create purchase orders or modify data.
Always provide clear, structured pricing information.""",
    tools=[
        FunctionTool(get_quotes),
        FunctionTool(get_price_history),
        FunctionTool(check_inventory),
    ],
)


# ── A2A Server wrapper ─────────────────────────────────────────────

class PricingAgentExecutor(AgentExecutor):
    """Wraps the ADK agent as an A2A-compatible executor."""

    async def execute(self, context, event_queue):
        """Execute the pricing agent with delegation validation."""
        user_message = ""
        delegation_ctx = {}

        for part in context.message.parts:
            if hasattr(part, "text"):
                user_message = part.text
            if hasattr(part, "metadata"):
                delegation_ctx = part.metadata.get(
                    "delegation_context", {}
                )

        # Validate delegation chain
        try:
            if delegation_ctx:
                chain = validate_incoming_delegation(
                    delegation_ctx
                )
                logger.info(
                    "Delegation validated: depth=%d, read_only=%s",
                    chain.depth,
                    chain.current_scope().read_only,
                )
        except ValidationError as e:
            auditor.log_validation_failure(
                "pricing-agent", str(e), delegation_ctx
            )
            from a2a.types import (
                TaskState,
                TaskStatus,
                Artifact,
                Part,
                TextPart,
            )
            error_artifact = Artifact(
                parts=[
                    Part(
                        root=TextPart(
                            text=f"DELEGATION DENIED: {e}"
                        )
                    )
                ]
            )
            event_queue.enqueue_event(error_artifact)
            return

        # Run the ADK agent
        response = pricing_agent.invoke(user_message)
        from a2a.types import Artifact, Part, TextPart
        result_artifact = Artifact(
            parts=[
                Part(
                    root=TextPart(
                        text=str(response.get("output", ""))
                    )
                )
            ]
        )
        event_queue.enqueue_event(result_artifact)

    async def cancel(self, context, event_queue):
        pass


# ── Agent Card & A2A Server ────────────────────────────────────────

agent_card = AgentCard(
    name="Pricing Agent",
    description="Read-only pricing and inventory research specialist",
    url="http://0.0.0.0:9000/",
    version="1.0.0",
    skills=[
        AgentSkill(
            id="get-quotes",
            name="Get Price Quotes",
            description="Get price quotes for a product SKU",
        ),
        AgentSkill(
            id="get-history",
            name="Get Price History",
            description="Get historical pricing data",
        ),
        AgentSkill(
            id="check-inventory",
            name="Check Inventory",
            description="Check available stock levels",
        ),
    ],
    capabilities=AgentCapabilities(streaming=False),
)

request_handler = DefaultRequestHandler(
    agent_executor=PricingAgentExecutor(),
    task_store=None,
)

a2a_app = A2AStarletteApplication(
    agent_card=agent_card,
    http_handler=request_handler,
)

if __name__ == "__main__":
    # Bind to all interfaces — required for Docker container networking
    host = os.environ.get("HOST", "0.0.0.0")  # nosec B104
    uvicorn.run(
        a2a_app.build(),
        host=host,
        port=9000,
    )
