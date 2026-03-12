"""Core data models for delegation chains."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fnmatch import fnmatch


@dataclass(frozen=True)
class DelegationScope:
    """Defines the attenuated permissions for a delegated agent.

    Scopes are immutable. To narrow a scope, use attenuate_scope() which
    returns a new DelegationScope with tighter constraints.
    """

    allowed_tools: tuple[str, ...] = ()
    max_amount: float | None = None
    allowed_regions: tuple[str, ...] = ()
    read_only: bool = False
    custom_constraints: tuple[tuple[str, str], ...] = ()

    def tool_permitted(self, tool_name: str) -> bool:
        """Check if a tool name matches any allowed tool pattern (supports globs)."""
        return any(fnmatch(tool_name, pattern) for pattern in self.allowed_tools)

    def amount_permitted(self, amount: float) -> bool:
        """Check if an amount is within the delegated cap."""
        if self.max_amount is None:
            return True
        return amount <= self.max_amount

    def region_permitted(self, region: str) -> bool:
        """Check if a region is allowed. Empty means all regions allowed."""
        if not self.allowed_regions:
            return True
        return region in self.allowed_regions

    def to_dict(self) -> dict:
        return {
            "allowed_tools": list(self.allowed_tools),
            "max_amount": self.max_amount,
            "allowed_regions": list(self.allowed_regions),
            "read_only": self.read_only,
            "custom_constraints": dict(self.custom_constraints),
        }


@dataclass(frozen=True)
class DelegationToken:
    """Immutable token encoding a single delegation hop."""

    token_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    parent_agent_id: str = ""
    child_agent_id: str = ""
    scope: DelegationScope = field(default_factory=DelegationScope)
    issued_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    expires_at: str = ""
    chain_hash: str = ""

    def to_dict(self) -> dict:
        return {
            "token_id": self.token_id,
            "parent_agent_id": self.parent_agent_id,
            "child_agent_id": self.child_agent_id,
            "scope": self.scope.to_dict(),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "chain_hash": self.chain_hash,
        }


def _compute_chain_hash(token_dict: dict) -> str:
    """Compute a SHA-256 hash of a token dict for tamper detection."""
    canonical = json.dumps(token_dict, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass
class DelegationChain:
    """Ordered list of delegation hops from originator to current agent."""

    originating_user: str = ""
    hops: list[DelegationToken] = field(default_factory=list)

    def add_hop(self, token: DelegationToken) -> DelegationToken:
        """Append a hop, computing its chain_hash from the previous hop.

        Returns the token (potentially with updated chain_hash) that was added.
        """
        chain_hash = ""
        if self.hops:
            previous = self.hops[-1]
            chain_hash = _compute_chain_hash(previous.to_dict())

        # Since DelegationToken is frozen, create a new one with the hash set
        token = DelegationToken(
            token_id=token.token_id,
            parent_agent_id=token.parent_agent_id,
            child_agent_id=token.child_agent_id,
            scope=token.scope,
            issued_at=token.issued_at,
            expires_at=token.expires_at,
            chain_hash=chain_hash,
        )
        self.hops.append(token)
        return token

    @property
    def depth(self) -> int:
        return len(self.hops)

    def current_scope(self) -> DelegationScope:
        """Returns the scope of the most recent hop (the tightest scope)."""
        if not self.hops:
            return DelegationScope()
        return self.hops[-1].scope

    def verify_integrity(self) -> bool:
        """Verify that every chain_hash matches the previous hop's content."""
        for i, hop in enumerate(self.hops):
            if i == 0:
                if hop.chain_hash != "":
                    return False
            else:
                expected = _compute_chain_hash(self.hops[i - 1].to_dict())
                if hop.chain_hash != expected:
                    return False
        return True

    def to_cedar_context(self) -> dict:
        """Converts the chain into Cedar evaluation context."""
        scope = self.current_scope()
        return {
            "delegationChain": [
                {
                    "from": hop.parent_agent_id,
                    "to": hop.child_agent_id,
                    "scope": hop.scope.to_dict(),
                    "tokenId": hop.token_id,
                }
                for hop in self.hops
            ],
            "chainDepth": self.depth,
            "originatingUser": self.originating_user,
            "allowedTools": list(scope.allowed_tools),
            "maxAmount": scope.max_amount,
            "readOnly": scope.read_only,
        }

    def to_dict(self) -> dict:
        return {
            "originating_user": self.originating_user,
            "hops": [h.to_dict() for h in self.hops],
        }
