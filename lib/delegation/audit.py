"""Structured audit logging for delegation chain events.

Emits JSON-structured log entries that match the CloudWatch Logs Insights
queries in infra/queries/audit-queries.sql and the dashboard metric filters
in infra/lib/delegation-audit-dashboard.ts.

Usage:
    from lib.delegation.audit import DelegationAuditor

    auditor = DelegationAuditor(agent_id="pricing-agent")
    auditor.log_tool_invocation(
        chain=chain,
        tool="pricing-api__get_quotes",
        tool_input={"sku": "SKU-4821", "quantity": 500},
        decision="ALLOW",
        policy_decisions=[
            {"policyId": "policy-002", "effect": "PERMIT", "reason": "..."}
        ],
    )
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from lib.delegation.models import DelegationChain

# CloudWatch-compatible JSON formatter
_LOG_GROUP = os.environ.get(
    "DELEGATION_LOG_GROUP", "/agentic-delegation/audit"
)


class _JsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON for CloudWatch ingestion."""

    def format(self, record: logging.LogRecord) -> str:
        if isinstance(record.msg, dict):
            return json.dumps(record.msg, default=str)
        return json.dumps({"message": record.getMessage()}, default=str)


def _get_audit_logger() -> logging.Logger:
    """Get or create the audit logger with JSON formatting."""
    logger = logging.getLogger("agentic_delegation.audit")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


class DelegationAuditor:
    """Emits structured audit events for delegation chain activity.

    Each event includes the full delegation chain, the decision, and
    enough context for CloudWatch Logs Insights queries to slice by
    agent, tool, user, chain depth, and amount proximity to cap.
    """

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self._logger = _get_audit_logger()

    def log_tool_invocation(
        self,
        chain: DelegationChain,
        tool: str,
        tool_input: dict[str, Any],
        decision: str,
        policy_decisions: list[dict[str, str]] | None = None,
        reason: str = "",
    ) -> dict:
        """Log a tool invocation decision with full chain context.

        Args:
            chain: The delegation chain at time of invocation.
            tool: Tool name being invoked.
            tool_input: Arguments passed to the tool.
            decision: "ALLOW" or "DENY".
            policy_decisions: List of Cedar policy evaluation results.
            reason: Human-readable reason for the decision.

        Returns:
            The audit event dict (for testing/inspection).
        """
        scope = chain.current_scope()
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "eventType": "TOOL_INVOCATION",
            "decision": decision,
            "tool": tool,
            "agent": self.agent_id,
            "originatingUser": chain.originating_user,
            "chainDepth": chain.depth,
            "maxAmount": scope.max_amount,
            "readOnly": scope.read_only,
            "delegationChain": [
                {
                    "hop": i,
                    "from": hop.parent_agent_id,
                    "to": hop.child_agent_id,
                    "scopeGranted": list(hop.scope.allowed_tools),
                    "maxAmount": hop.scope.max_amount,
                }
                for i, hop in enumerate(chain.hops)
            ],
            "toolInput": tool_input,
            "policyDecisions": policy_decisions or [],
        }
        if reason:
            event["reason"] = reason

        self._logger.info(event)
        return event

    def log_delegation_mint(
        self,
        chain: DelegationChain,
        parent_id: str,
        child_id: str,
    ) -> dict:
        """Log a delegation token minting event."""
        scope = chain.current_scope()
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "eventType": "DELEGATION_MINT",
            "agent": parent_id,
            "delegatedTo": child_id,
            "originatingUser": chain.originating_user,
            "chainDepth": chain.depth,
            "scopeGranted": list(scope.allowed_tools),
            "maxAmount": scope.max_amount,
            "readOnly": scope.read_only,
        }
        self._logger.info(event)
        return event

    def log_validation_failure(
        self,
        agent_id: str,
        error: str,
        delegation_context: dict | None = None,
    ) -> dict:
        """Log a delegation validation failure."""
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "eventType": "VALIDATION_FAILURE",
            "decision": "DENY",
            "agent": agent_id,
            "reason": error,
            "delegationContext": delegation_context,
        }
        self._logger.info(event)
        return event
