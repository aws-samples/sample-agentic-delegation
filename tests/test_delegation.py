"""Tests for the delegation token library."""

import pytest
from datetime import datetime, timedelta, timezone

from lib.delegation.models import DelegationScope, DelegationToken, DelegationChain
from lib.delegation.attenuate import attenuate_scope, AttenuationError
from lib.delegation.mint import mint_delegation
from lib.delegation.validate import validate_delegation, ValidationError


# ── DelegationScope ──────────────────────────────────────────────────


class TestDelegationScope:
    def test_tool_permitted_exact(self):
        scope = DelegationScope(allowed_tools=("pricing-api", "inventory-read"))
        assert scope.tool_permitted("pricing-api")
        assert not scope.tool_permitted("po-create")

    def test_tool_permitted_glob(self):
        scope = DelegationScope(allowed_tools=("pricing-*",))
        assert scope.tool_permitted("pricing-api")
        assert scope.tool_permitted("pricing-history")
        assert not scope.tool_permitted("po-create")

    def test_amount_permitted(self):
        scope = DelegationScope(max_amount=10000.0)
        assert scope.amount_permitted(8500.0)
        assert scope.amount_permitted(10000.0)
        assert not scope.amount_permitted(10000.01)

    def test_amount_permitted_no_cap(self):
        scope = DelegationScope(max_amount=None)
        assert scope.amount_permitted(999999.0)

    def test_region_permitted(self):
        scope = DelegationScope(allowed_regions=("us-east-1", "eu-west-1"))
        assert scope.region_permitted("us-east-1")
        assert not scope.region_permitted("ap-southeast-1")

    def test_region_permitted_empty_means_all(self):
        scope = DelegationScope(allowed_regions=())
        assert scope.region_permitted("anything")

    def test_to_dict(self):
        scope = DelegationScope(
            allowed_tools=("a", "b"),
            max_amount=100.0,
            read_only=True,
        )
        d = scope.to_dict()
        assert d["allowed_tools"] == ["a", "b"]
        assert d["max_amount"] == 100.0
        assert d["read_only"] is True


# ── DelegationChain ──────────────────────────────────────────────────


class TestDelegationChain:
    def _make_chain(self) -> DelegationChain:
        chain = DelegationChain(originating_user="user@acme.com")
        scope1 = DelegationScope(
            allowed_tools=("pricing-*", "po-create"),
            max_amount=10000.0,
            allowed_regions=("us-east-1",),
        )
        token1 = DelegationToken(
            parent_agent_id="user",
            child_agent_id="coordinator",
            scope=scope1,
        )
        chain.add_hop(token1)
        return chain

    def test_add_hop_first(self):
        chain = self._make_chain()
        assert chain.depth == 1
        assert chain.hops[0].chain_hash == ""

    def test_add_hop_second_gets_hash(self):
        chain = self._make_chain()
        scope2 = DelegationScope(
            allowed_tools=("pricing-*",),
            max_amount=None,
            read_only=True,
        )
        token2 = DelegationToken(
            parent_agent_id="coordinator",
            child_agent_id="pricing-agent",
            scope=scope2,
        )
        chain.add_hop(token2)
        assert chain.depth == 2
        assert chain.hops[1].chain_hash != ""

    def test_verify_integrity_valid(self):
        chain = self._make_chain()
        scope2 = DelegationScope(allowed_tools=("pricing-*",), read_only=True)
        token2 = DelegationToken(
            parent_agent_id="coordinator",
            child_agent_id="pricing-agent",
            scope=scope2,
        )
        chain.add_hop(token2)
        assert chain.verify_integrity()

    def test_verify_integrity_tampered(self):
        chain = self._make_chain()
        scope2 = DelegationScope(allowed_tools=("pricing-*",), read_only=True)
        token2 = DelegationToken(
            parent_agent_id="coordinator",
            child_agent_id="pricing-agent",
            scope=scope2,
        )
        chain.add_hop(token2)

        # Tamper: replace first hop with different scope
        tampered_scope = DelegationScope(allowed_tools=("*",), max_amount=999999.0)
        chain.hops[0] = DelegationToken(
            parent_agent_id="user",
            child_agent_id="coordinator",
            scope=tampered_scope,
        )
        assert not chain.verify_integrity()

    def test_current_scope_returns_last_hop(self):
        chain = self._make_chain()
        scope2 = DelegationScope(allowed_tools=("pricing-*",), read_only=True)
        token2 = DelegationToken(
            parent_agent_id="coordinator",
            child_agent_id="pricing-agent",
            scope=scope2,
        )
        chain.add_hop(token2)
        assert chain.current_scope().read_only is True
        assert chain.current_scope().allowed_tools == ("pricing-*",)

    def test_to_cedar_context(self):
        chain = self._make_chain()
        ctx = chain.to_cedar_context()
        assert ctx["chainDepth"] == 1
        assert ctx["originatingUser"] == "user@acme.com"
        assert ctx["maxAmount"] == 10000.0
        assert "pricing-*" in ctx["allowedTools"]


# ── attenuate_scope ──────────────────────────────────────────────────


class TestAttenuateScope:
    def test_valid_narrowing(self):
        parent = DelegationScope(
            allowed_tools=("pricing-*", "po-create"),
            max_amount=10000.0,
            allowed_regions=("us-east-1", "eu-west-1"),
        )
        child = attenuate_scope(
            parent,
            allowed_tools=("pricing-api",),
            max_amount=5000.0,
            allowed_regions=("us-east-1",),
            read_only=True,
        )
        assert child.allowed_tools == ("pricing-api",)
        assert child.max_amount == 5000.0
        assert child.read_only is True

    def test_tool_not_in_parent_raises(self):
        parent = DelegationScope(allowed_tools=("pricing-*",))
        with pytest.raises(AttenuationError, match="po-create"):
            attenuate_scope(parent, allowed_tools=("po-create",))

    def test_amount_exceeds_parent_raises(self):
        parent = DelegationScope(allowed_tools=("a",), max_amount=100.0)
        with pytest.raises(AttenuationError, match="exceeds parent"):
            attenuate_scope(parent, allowed_tools=("a",), max_amount=200.0)

    def test_remove_amount_cap_raises(self):
        parent = DelegationScope(allowed_tools=("a",), max_amount=100.0)
        with pytest.raises(AttenuationError, match="Cannot remove amount cap"):
            attenuate_scope(parent, allowed_tools=("a",), max_amount=None)

    def test_region_not_in_parent_raises(self):
        parent = DelegationScope(
            allowed_tools=("a",), allowed_regions=("us-east-1",)
        )
        with pytest.raises(AttenuationError, match="ap-southeast-1"):
            attenuate_scope(
                parent, allowed_tools=("a",), allowed_regions=("ap-southeast-1",)
            )

    def test_remove_read_only_raises(self):
        parent = DelegationScope(allowed_tools=("a",), read_only=True)
        with pytest.raises(AttenuationError, match="read_only"):
            attenuate_scope(parent, allowed_tools=("a",), read_only=False)

    def test_missing_custom_constraint_raises(self):
        parent = DelegationScope(
            allowed_tools=("a",),
            custom_constraints=(("department", "finance"),),
        )
        with pytest.raises(AttenuationError, match="department"):
            attenuate_scope(parent, allowed_tools=("a",), custom_constraints=())


# ── mint_delegation ──────────────────────────────────────────────────


class TestMintDelegation:
    def test_mint_first_hop(self):
        chain = DelegationChain(originating_user="user@acme.com")
        scope = DelegationScope(
            allowed_tools=("pricing-*", "po-create"),
            max_amount=10000.0,
        )
        chain, token = mint_delegation(
            chain, "user", "coordinator", scope, ttl_seconds=600
        )
        assert chain.depth == 1
        assert token.parent_agent_id == "user"
        assert token.child_agent_id == "coordinator"
        assert token.expires_at != ""

    def test_mint_second_hop_valid(self):
        chain = DelegationChain(originating_user="user@acme.com")
        scope1 = DelegationScope(
            allowed_tools=("pricing-*", "po-create"),
            max_amount=10000.0,
        )
        chain, _ = mint_delegation(chain, "user", "coordinator", scope1)

        scope2 = DelegationScope(
            allowed_tools=("pricing-api",),
            max_amount=5000.0,
            read_only=True,
        )
        chain, token2 = mint_delegation(
            chain, "coordinator", "pricing-agent", scope2
        )
        assert chain.depth == 2
        assert token2.chain_hash != ""

    def test_mint_exceeding_scope_raises(self):
        chain = DelegationChain(originating_user="user@acme.com")
        scope1 = DelegationScope(
            allowed_tools=("pricing-*",), max_amount=1000.0
        )
        chain, _ = mint_delegation(chain, "user", "coordinator", scope1)

        scope2 = DelegationScope(
            allowed_tools=("pricing-*",), max_amount=5000.0
        )
        with pytest.raises(AttenuationError):
            mint_delegation(chain, "coordinator", "child", scope2)

    def test_mint_wrong_parent_raises(self):
        chain = DelegationChain(originating_user="user@acme.com")
        scope1 = DelegationScope(allowed_tools=("a",))
        chain, _ = mint_delegation(chain, "user", "coordinator", scope1)

        scope2 = DelegationScope(allowed_tools=("a",))
        with pytest.raises(ValueError, match="doesn't match chain tip"):
            mint_delegation(chain, "wrong-agent", "child", scope2)


# ── validate_delegation ──────────────────────────────────────────────


class TestValidateDelegation:
    def test_valid_chain(self):
        chain = DelegationChain(originating_user="user@acme.com")
        scope = DelegationScope(allowed_tools=("a",))
        chain, _ = mint_delegation(chain, "user", "coordinator", scope)
        token = validate_delegation(chain, "coordinator")
        assert token.child_agent_id == "coordinator"

    def test_empty_chain_raises(self):
        chain = DelegationChain()
        with pytest.raises(ValidationError, match="empty"):
            validate_delegation(chain, "anyone")

    def test_wrong_child_raises(self):
        chain = DelegationChain(originating_user="user@acme.com")
        scope = DelegationScope(allowed_tools=("a",))
        chain, _ = mint_delegation(chain, "user", "coordinator", scope)
        with pytest.raises(ValidationError, match="not 'imposter'"):
            validate_delegation(chain, "imposter")

    def test_expired_token_raises(self):
        chain = DelegationChain(originating_user="user@acme.com")
        scope = DelegationScope(allowed_tools=("a",))
        chain, _ = mint_delegation(
            chain, "user", "coordinator", scope, ttl_seconds=0
        )
        # Token expires immediately (ttl=0 means expires_at == issued_at)
        # We need to manually set an expired time
        expired = datetime.now(timezone.utc) - timedelta(hours=1)
        chain.hops[-1] = DelegationToken(
            token_id=chain.hops[-1].token_id,
            parent_agent_id="user",
            child_agent_id="coordinator",
            scope=scope,
            issued_at=chain.hops[-1].issued_at,
            expires_at=expired.isoformat(),
            chain_hash=chain.hops[-1].chain_hash,
        )
        with pytest.raises(ValidationError, match="expired"):
            validate_delegation(chain, "coordinator")

    def test_tampered_chain_raises(self):
        chain = DelegationChain(originating_user="user@acme.com")
        scope1 = DelegationScope(allowed_tools=("a",))
        chain, _ = mint_delegation(chain, "user", "coordinator", scope1)
        scope2 = DelegationScope(allowed_tools=("a",))
        chain, _ = mint_delegation(chain, "coordinator", "child", scope2)

        # Tamper with first hop
        chain.hops[0] = DelegationToken(
            parent_agent_id="user",
            child_agent_id="coordinator",
            scope=DelegationScope(allowed_tools=("*",)),
        )
        with pytest.raises(ValidationError, match="integrity"):
            validate_delegation(chain, "child")
