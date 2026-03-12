"""End-to-end integration tests for the delegation chain flow.

Exercises the full procurement scenario:
  User → Coordinator → Pricing Agent (read-only)
  User → Coordinator → Purchasing Agent (amount-capped)

These tests run without AWS credentials — they validate the
delegation library logic that would execute inside the agents.
"""

import json
import copy
import pytest

from lib.delegation.models import (
    DelegationChain,
    DelegationScope,
    DelegationToken,
)
from lib.delegation.mint import mint_delegation
from lib.delegation.validate import validate_delegation, ValidationError
from lib.delegation.attenuate import AttenuationError


class TestProcurementWorkflowE2E:
    """Full procurement scenario from the spec."""

    def _build_root_chain(self) -> DelegationChain:
        """Simulate: User grants coordinator full procurement scope."""
        chain = DelegationChain(
            originating_user="procurement-manager@acme.com"
        )
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
        )
        return chain

    def _delegate_to_pricing(
        self, chain: DelegationChain
    ) -> DelegationChain:
        """Coordinator delegates read-only to pricing agent."""
        pricing_chain = copy.deepcopy(chain)
        pricing_scope = DelegationScope(
            allowed_tools=("pricing-api*", "inventory-read*"),
            max_amount=10000.0,  # inherit parent cap
            read_only=True,
            allowed_regions=("us-east-1",),
        )
        mint_delegation(
            pricing_chain,
            parent_agent_id="coordinator",
            child_agent_id="pricing-agent",
            child_scope=pricing_scope,
        )
        return pricing_chain

    def _delegate_to_purchasing(
        self, chain: DelegationChain
    ) -> DelegationChain:
        """Coordinator delegates write to purchasing agent."""
        purchasing_chain = copy.deepcopy(chain)
        purchasing_scope = DelegationScope(
            allowed_tools=("po-create*",),
            max_amount=10000.0,
            allowed_regions=("us-east-1",),
        )
        mint_delegation(
            purchasing_chain,
            parent_agent_id="coordinator",
            child_agent_id="purchasing-agent",
            child_scope=purchasing_scope,
        )
        return purchasing_chain

    # ── Happy path ─────────────────────────────────────────────────

    def test_full_pricing_flow(self):
        """User → Coordinator → Pricing Agent: read-only research."""
        chain = self._build_root_chain()
        pricing_chain = self._delegate_to_pricing(chain)

        # Pricing agent validates its delegation
        token = validate_delegation(pricing_chain, "pricing-agent")
        assert token.child_agent_id == "pricing-agent"

        # Scope checks
        scope = pricing_chain.current_scope()
        assert scope.read_only is True
        assert scope.tool_permitted("pricing-api__get_quotes")
        assert scope.tool_permitted("inventory-read__check")
        assert not scope.tool_permitted("po-create__create_po")

        # Cedar context is well-formed
        ctx = pricing_chain.to_cedar_context()
        assert ctx["chainDepth"] == 2
        assert ctx["readOnly"] is True
        assert ctx["originatingUser"] == "procurement-manager@acme.com"

    def test_full_purchasing_flow_under_cap(self):
        """User → Coordinator → Purchasing Agent: PO under $10k."""
        chain = self._build_root_chain()
        purchasing_chain = self._delegate_to_purchasing(chain)

        token = validate_delegation(
            purchasing_chain, "purchasing-agent"
        )
        scope = purchasing_chain.current_scope()

        assert scope.tool_permitted("po-create__create_po")
        assert scope.amount_permitted(8500.0)
        assert not scope.read_only

        ctx = purchasing_chain.to_cedar_context()
        assert ctx["chainDepth"] == 2
        assert ctx["maxAmount"] == 10000.0

    def test_full_purchasing_flow_over_cap(self):
        """User → Coordinator → Purchasing Agent: PO over $10k denied."""
        chain = self._build_root_chain()
        purchasing_chain = self._delegate_to_purchasing(chain)

        scope = purchasing_chain.current_scope()
        assert not scope.amount_permitted(12000.0)

    # ── Scope enforcement ──────────────────────────────────────────

    def test_pricing_agent_cannot_create_po(self):
        """Pricing agent's read-only scope blocks po-create."""
        chain = self._build_root_chain()
        pricing_chain = self._delegate_to_pricing(chain)
        scope = pricing_chain.current_scope()

        assert not scope.tool_permitted("po-create__create_po")
        assert scope.read_only is True

    def test_purchasing_agent_cannot_read_pricing(self):
        """Purchasing agent's scope blocks pricing tools."""
        chain = self._build_root_chain()
        purchasing_chain = self._delegate_to_purchasing(chain)
        scope = purchasing_chain.current_scope()

        assert not scope.tool_permitted("pricing-api__get_quotes")
        assert scope.tool_permitted("po-create__create_po")

    # ── Chain integrity ────────────────────────────────────────────

    def test_chain_integrity_across_hops(self):
        """Hash chain is valid across all hops."""
        chain = self._build_root_chain()
        pricing_chain = self._delegate_to_pricing(chain)
        assert pricing_chain.verify_integrity()

    def test_tampered_chain_detected(self):
        """Tampering with any hop breaks integrity."""
        chain = self._build_root_chain()
        pricing_chain = self._delegate_to_pricing(chain)

        # Tamper: widen the root scope
        pricing_chain.hops[0] = DelegationToken(
            parent_agent_id="user",
            child_agent_id="coordinator",
            scope=DelegationScope(
                allowed_tools=("*",), max_amount=999999.0
            ),
        )
        assert not pricing_chain.verify_integrity()

    # ── Identity validation ────────────────────────────────────────

    def test_wrong_agent_rejected(self):
        """Pricing chain rejects validation by purchasing agent."""
        chain = self._build_root_chain()
        pricing_chain = self._delegate_to_pricing(chain)

        with pytest.raises(ValidationError, match="not 'purchasing-agent'"):
            validate_delegation(pricing_chain, "purchasing-agent")

    # ── Attenuation enforcement ────────────────────────────────────

    def test_coordinator_cannot_escalate_scope(self):
        """Coordinator can't delegate broader scope than it has."""
        chain = self._build_root_chain()
        escalated_scope = DelegationScope(
            allowed_tools=("*",),  # broader than parent
            max_amount=50000.0,    # higher than parent
        )
        with pytest.raises(AttenuationError):
            mint_delegation(
                chain,
                parent_agent_id="coordinator",
                child_agent_id="rogue-agent",
                child_scope=escalated_scope,
            )

    def test_three_hop_chain(self):
        """User → Coordinator → Pricing → Sub-analyst (3 hops)."""
        chain = self._build_root_chain()
        pricing_chain = self._delegate_to_pricing(chain)

        # Pricing agent delegates further to a sub-analyst
        sub_chain = copy.deepcopy(pricing_chain)
        sub_scope = DelegationScope(
            allowed_tools=("pricing-api*",),
            max_amount=10000.0,  # inherit parent cap
            read_only=True,
            allowed_regions=("us-east-1",),
        )
        mint_delegation(
            sub_chain,
            parent_agent_id="pricing-agent",
            child_agent_id="sub-analyst",
            child_scope=sub_scope,
        )

        assert sub_chain.depth == 3
        assert sub_chain.verify_integrity()
        token = validate_delegation(sub_chain, "sub-analyst")
        assert token.child_agent_id == "sub-analyst"

        # Sub-analyst still can't create POs
        assert not sub_chain.current_scope().tool_permitted("po-create*")

    # ── Cedar context output ───────────────────────────────────────

    def test_cedar_context_structure(self):
        """Cedar context has all required fields for policy evaluation."""
        chain = self._build_root_chain()
        purchasing_chain = self._delegate_to_purchasing(chain)
        ctx = purchasing_chain.to_cedar_context()

        # Required fields
        assert "delegationChain" in ctx
        assert "chainDepth" in ctx
        assert "originatingUser" in ctx
        assert "allowedTools" in ctx
        assert "maxAmount" in ctx
        assert "readOnly" in ctx

        # Chain structure
        assert len(ctx["delegationChain"]) == 2
        hop0 = ctx["delegationChain"][0]
        assert hop0["from"] == "user"
        assert hop0["to"] == "coordinator"
        hop1 = ctx["delegationChain"][1]
        assert hop1["from"] == "coordinator"
        assert hop1["to"] == "purchasing-agent"

    def test_cedar_context_serializable(self):
        """Cedar context can be JSON-serialized (for A2A metadata)."""
        chain = self._build_root_chain()
        pricing_chain = self._delegate_to_pricing(chain)
        ctx = pricing_chain.to_cedar_context()

        serialized = json.dumps(ctx)
        deserialized = json.loads(serialized)
        assert deserialized["chainDepth"] == 2
        assert deserialized["readOnly"] is True
