"""Purchasing Agent — LangGraph-based A2A server on AgentCore Runtime.

Write-capable specialist that creates purchase orders.
Validates delegation tokens and enforces amount caps before acting.
"""

import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Annotated, TypedDict

from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langchain_core.messages import HumanMessage, AIMessage
from a2a.server.apps.starlette import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.agent_execution import AgentExecutor
from a2a.types import (
    AgentCard,
    AgentSkill,
    AgentCapabilities,
    Artifact,
    Part,
    TextPart,
)
import uvicorn

# Import delegation library
sys.path.insert(0, "/opt")
from lib.delegation.models import (
    DelegationChain,
    DelegationToken,
    DelegationScope,
)
from lib.delegation.validate import (
    validate_delegation,
    ValidationError,
)
from lib.delegation.audit import DelegationAuditor

logger = logging.getLogger(__name__)
auditor = DelegationAuditor("purchasing-agent")

GATEWAY_URL = os.environ.get("GATEWAY_URL", "")


# ── Delegation validation ──────────────────────────────────────────

def validate_incoming_delegation(
    delegation_context: dict,
) -> DelegationChain:
    """Reconstruct and validate a delegation chain."""
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

    validate_delegation(chain, "purchasing-agent")
    return chain


# ── LangGraph State & Nodes ────────────────────────────────────────

class PurchasingState(TypedDict):
    messages: Annotated[list, add_messages]
    sku: str
    quantity: int
    amount: float
    delegation_context: dict
    result: dict


def validate_node(state: PurchasingState) -> dict:
    """Validate the delegation chain and check amount cap."""
    delegation_ctx = state.get("delegation_context", {})
    amount = state.get("amount", 0)

    try:
        chain = validate_incoming_delegation(delegation_ctx)
    except ValidationError as e:
        auditor.log_validation_failure(
            "purchasing-agent", str(e), delegation_ctx
        )
        return {
            "result": {
                "status": "DENIED",
                "reason": f"Delegation validation failed: {e}",
            },
            "messages": [
                AIMessage(
                    content=f"DENIED: {e}"
                )
            ],
        }

    scope = chain.current_scope()

    # Check read-only constraint
    if scope.read_only:
        return {
            "result": {
                "status": "DENIED",
                "reason": "Read-only delegation cannot create POs",
            },
            "messages": [
                AIMessage(
                    content="DENIED: read-only scope"
                )
            ],
        }

    # Check tool permission
    if not scope.tool_permitted("po-create"):
        return {
            "result": {
                "status": "DENIED",
                "reason": "po-create not in delegated tools",
            },
            "messages": [
                AIMessage(
                    content="DENIED: tool not permitted"
                )
            ],
        }

    # Check amount cap
    if not scope.amount_permitted(amount):
        return {
            "result": {
                "status": "DENIED",
                "reason": (
                    f"Amount ${amount} exceeds delegated "
                    f"cap ${scope.max_amount}"
                ),
            },
            "messages": [
                AIMessage(
                    content=(
                        f"DENIED: ${amount} > cap "
                        f"${scope.max_amount}"
                    )
                )
            ],
        }

    return {
        "messages": [
            AIMessage(content="Delegation validated, proceeding")
        ]
    }


def create_po_node(state: PurchasingState) -> dict:
    """Create the purchase order (mock implementation)."""
    # Check if validation already denied
    result = state.get("result", {})
    if result.get("status") == "DENIED":
        return {}

    po = {
        "po_id": f"PO-{uuid.uuid4().hex[:8].upper()}",
        "sku": state["sku"],
        "quantity": state["quantity"],
        "amount": state["amount"],
        "status": "CREATED",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "delegation_chain_depth": len(
            state.get("delegation_context", {}).get(
                "delegationChain", []
            )
        ),
    }

    return {
        "result": po,
        "messages": [
            AIMessage(
                content=f"PO created: {po['po_id']}"
            )
        ],
    }


def should_create_po(state: PurchasingState) -> str:
    """Route: skip PO creation if validation denied."""
    result = state.get("result", {})
    if result.get("status") == "DENIED":
        return END
    return "create_po"


# Build the graph
graph_builder = StateGraph(PurchasingState)
graph_builder.add_node("validate", validate_node)
graph_builder.add_node("create_po", create_po_node)
graph_builder.set_entry_point("validate")
graph_builder.add_conditional_edges(
    "validate", should_create_po
)
graph_builder.add_edge("create_po", END)

purchasing_graph = graph_builder.compile()


# ── A2A Server wrapper ─────────────────────────────────────────────

class PurchasingAgentExecutor(AgentExecutor):
    """Wraps the LangGraph as an A2A-compatible executor."""

    async def execute(self, context, event_queue):
        user_message = ""
        delegation_ctx = {}
        task_data = {}

        for part in context.message.parts:
            if hasattr(part, "text"):
                user_message = part.text
            if hasattr(part, "metadata"):
                meta = part.metadata or {}
                delegation_ctx = meta.get(
                    "delegation_context", {}
                )
                task_data = meta.get("task", {})

        # Parse task parameters from message or metadata
        sku = task_data.get("sku", "")
        quantity = task_data.get("quantity", 0)
        amount = task_data.get("amount", 0)

        # If not in metadata, try parsing from text
        if not sku and user_message:
            try:
                parsed = json.loads(user_message)
                sku = parsed.get("sku", "")
                quantity = parsed.get("quantity", 0)
                amount = parsed.get("amount", 0)
            except (json.JSONDecodeError, AttributeError):
                pass

        # Run the LangGraph
        initial_state = {
            "messages": [HumanMessage(content=user_message)],
            "sku": sku,
            "quantity": quantity,
            "amount": amount,
            "delegation_context": delegation_ctx,
            "result": {},
        }

        final_state = purchasing_graph.invoke(initial_state)
        result = final_state.get("result", {})

        result_artifact = Artifact(
            parts=[
                Part(
                    root=TextPart(
                        text=json.dumps(result, indent=2)
                    )
                )
            ]
        )
        event_queue.enqueue_event(result_artifact)

    async def cancel(self, context, event_queue):
        pass


# ── Agent Card & A2A Server ────────────────────────────────────────

agent_card = AgentCard(
    name="Purchasing Agent",
    description=(
        "Purchase order creation specialist — "
        "write access, amount-capped by delegation"
    ),
    url="http://0.0.0.0:9000/",
    version="1.0.0",
    skills=[
        AgentSkill(
            id="create-po",
            name="Create Purchase Order",
            description=(
                "Create a PO for a product SKU, "
                "subject to delegated amount cap"
            ),
        ),
    ],
    capabilities=AgentCapabilities(streaming=False),
)

request_handler = DefaultRequestHandler(
    agent_executor=PurchasingAgentExecutor(),
    task_store=None,
)

a2a_app = A2AStarletteApplication(
    agent_card=agent_card,
    http_handler=request_handler,
)

if __name__ == "__main__":
    uvicorn.run(
        a2a_app.build(),
        host="0.0.0.0",
        port=9000,
    )
