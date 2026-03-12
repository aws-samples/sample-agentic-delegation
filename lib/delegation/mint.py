"""Minting delegation tokens — parent agent creates scoped tokens for children."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from lib.delegation.attenuate import attenuate_scope
from lib.delegation.models import DelegationChain, DelegationScope, DelegationToken


def mint_delegation(
    chain: DelegationChain,
    parent_agent_id: str,
    child_agent_id: str,
    child_scope: DelegationScope,
    ttl_seconds: int = 3600,
) -> tuple[DelegationChain, DelegationToken]:
    """Mint a new delegation token and append it to the chain.

    Validates that child_scope is a strict subset of the current chain scope
    before minting. Returns a new chain (with the hop appended) and the token.

    Args:
        chain: The current delegation chain.
        parent_agent_id: ID of the agent doing the delegating.
        child_agent_id: ID of the agent receiving delegated authority.
        child_scope: The narrowed scope for the child agent.
        ttl_seconds: How long the delegation is valid (default 1 hour).

    Returns:
        Tuple of (updated chain, minted token).

    Raises:
        AttenuationError: If child_scope exceeds the current chain scope.
        ValueError: If parent_agent_id doesn't match the last hop's child.
    """
    # Validate parent identity matches chain tip
    if chain.hops:
        chain_tip = chain.hops[-1].child_agent_id
        if chain_tip and chain_tip != parent_agent_id:
            raise ValueError(
                f"parent_agent_id '{parent_agent_id}' doesn't match chain tip '{chain_tip}'"
            )

    # Validate attenuation — this raises AttenuationError if scope is too broad
    current = chain.current_scope()
    if chain.hops:
        attenuate_scope(
            current,
            allowed_tools=child_scope.allowed_tools,
            max_amount=child_scope.max_amount,
            allowed_regions=child_scope.allowed_regions,
            read_only=child_scope.read_only,
            custom_constraints=child_scope.custom_constraints,
        )

    now = datetime.now(timezone.utc)
    token = DelegationToken(
        parent_agent_id=parent_agent_id,
        child_agent_id=child_agent_id,
        scope=child_scope,
        issued_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=ttl_seconds)).isoformat(),
    )

    token = chain.add_hop(token)
    return chain, token
