"""Tests for the delegation audit logging module."""

import json
import logging

from lib.delegation.models import DelegationChain, DelegationScope
from lib.delegation.mint import mint_delegation
from lib.delegation.audit import DelegationAuditor


class TestDelegationAuditor:
    def _make_chain(self) -> DelegationChain:
        chain = DelegationChain(originating_user="user@acme.com")
        scope = DelegationScope(
            allowed_tools=("pricing-*", "po-create"),
            max_amount=10000.0,
            allowed_regions=("us-east-1",),
        )
        mint_delegation(chain, "user", "coordinator", scope)
        return chain

    def test_log_tool_invocation_allow(self):
        chain = self._make_chain()
        auditor = DelegationAuditor("coordinator")
        event = auditor.log_tool_invocation(
            chain=chain,
            tool="pricing-api__get_quotes",
            tool_input={"sku": "SKU-4821", "quantity": 500},
            decision="ALLOW",
        )
        assert event["decision"] == "ALLOW"
        assert event["tool"] == "pricing-api__get_quotes"
        assert event["agent"] == "coordinator"
        assert event["chainDepth"] == 1
        assert event["originatingUser"] == "user@acme.com"
        assert len(event["delegationChain"]) == 1

    def test_log_tool_invocation_deny(self):
        chain = self._make_chain()
        auditor = DelegationAuditor("coordinator")
        event = auditor.log_tool_invocation(
            chain=chain,
            tool="po-create",
            tool_input={"amount": 50000},
            decision="DENY",
            reason="Amount exceeds cap",
        )
        assert event["decision"] == "DENY"
        assert event["reason"] == "Amount exceeds cap"

    def test_log_tool_invocation_with_policies(self):
        chain = self._make_chain()
        auditor = DelegationAuditor("coordinator")
        policies = [
            {"policyId": "p-001", "effect": "PERMIT", "reason": "ok"},
            {"policyId": "p-003", "effect": "NOT_APPLICABLE", "reason": "n/a"},
        ]
        event = auditor.log_tool_invocation(
            chain=chain,
            tool="pricing-api__get_quotes",
            tool_input={},
            decision="ALLOW",
            policy_decisions=policies,
        )
        assert len(event["policyDecisions"]) == 2

    def test_log_delegation_mint(self):
        chain = self._make_chain()
        auditor = DelegationAuditor("coordinator")
        event = auditor.log_delegation_mint(chain, "user", "coordinator")
        assert event["eventType"] == "DELEGATION_MINT"
        assert event["agent"] == "user"
        assert event["delegatedTo"] == "coordinator"

    def test_log_validation_failure(self):
        auditor = DelegationAuditor("pricing-agent")
        event = auditor.log_validation_failure(
            "pricing-agent",
            "Chain integrity check failed",
            {"chainDepth": 2},
        )
        assert event["eventType"] == "VALIDATION_FAILURE"
        assert event["decision"] == "DENY"
        assert "integrity" in event["reason"]

    def test_event_is_json_serializable(self):
        chain = self._make_chain()
        auditor = DelegationAuditor("coordinator")
        event = auditor.log_tool_invocation(
            chain=chain,
            tool="test",
            tool_input={"key": "value"},
            decision="ALLOW",
        )
        serialized = json.dumps(event, default=str)
        deserialized = json.loads(serialized)
        assert deserialized["tool"] == "test"

    def test_delegation_chain_hops_in_event(self):
        """Multi-hop chain produces correct audit trail."""
        chain = DelegationChain(originating_user="user@acme.com")
        scope1 = DelegationScope(
            allowed_tools=("pricing-*",), max_amount=10000.0
        )
        mint_delegation(chain, "user", "coordinator", scope1)
        scope2 = DelegationScope(
            allowed_tools=("pricing-api",), max_amount=5000.0, read_only=True
        )
        mint_delegation(chain, "coordinator", "pricing-agent", scope2)

        auditor = DelegationAuditor("pricing-agent")
        event = auditor.log_tool_invocation(
            chain=chain,
            tool="pricing-api",
            tool_input={},
            decision="ALLOW",
        )
        assert event["chainDepth"] == 2
        assert event["delegationChain"][0]["from"] == "user"
        assert event["delegationChain"][1]["from"] == "coordinator"
        assert event["readOnly"] is True
