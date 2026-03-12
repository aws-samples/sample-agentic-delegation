"""Validation of delegation tokens and chains."""

from __future__ import annotations

from datetime import datetime, timezone

from lib.delegation.models import DelegationChain, DelegationToken


class ValidationError(Exception):
    """Raised when a delegation token or chain fails validation."""


def validate_delegation(
    chain: DelegationChain,
    expected_child_id: str,
) -> DelegationToken:
    """Validate a delegation chain from the perspective of the receiving agent.

    Checks:
    1. Chain is non-empty
    2. Chain integrity (hash chain is unbroken)
    3. The latest hop is addressed to expected_child_id
    4. The latest hop has not expired

    Returns the validated token (the last hop) on success.

    Raises:
        ValidationError on any check failure.
    """
    if not chain.hops:
        raise ValidationError("Delegation chain is empty — no authority granted")

    if not chain.verify_integrity():
        raise ValidationError("Delegation chain integrity check failed — possible tampering")

    latest = chain.hops[-1]

    if latest.child_agent_id != expected_child_id:
        raise ValidationError(
            f"Token is for '{latest.child_agent_id}', not '{expected_child_id}'"
        )

    if latest.expires_at:
        try:
            expiry = datetime.fromisoformat(latest.expires_at)
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > expiry:
                raise ValidationError(
                    f"Delegation token expired at {latest.expires_at}"
                )
        except ValueError as e:
            raise ValidationError(f"Invalid expires_at format: {e}") from e

    return latest
