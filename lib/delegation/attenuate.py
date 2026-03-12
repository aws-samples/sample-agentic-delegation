"""Scope attenuation — narrowing a parent scope into a child scope."""

from __future__ import annotations

from fnmatch import fnmatch

from lib.delegation.models import DelegationScope

_UNSET = object()


class AttenuationError(Exception):
    """Raised when a requested child scope exceeds the parent scope."""


def attenuate_scope(
    parent_scope: DelegationScope,
    *,
    allowed_tools: tuple[str, ...] | None = None,
    max_amount: float | None | object = _UNSET,
    allowed_regions: tuple[str, ...] | None = None,
    read_only: bool | None = None,
    custom_constraints: tuple[tuple[str, str], ...] | None = None,
) -> DelegationScope:
    """Create a new scope that is a strict subset of the parent scope.

    Every field in the child scope must be equal to or more restrictive than
    the parent. Raises AttenuationError if the child would exceed the parent.
    """

    # --- Tools: child tools must each match at least one parent pattern ---
    child_tools = allowed_tools if allowed_tools is not None else parent_scope.allowed_tools
    for tool in child_tools:
        if not any(fnmatch(tool, p) for p in parent_scope.allowed_tools):
            # Allow if the child tool is itself a narrower glob of a parent glob
            # e.g., parent has "pricing-*", child requests "pricing-api" — that's fine
            # But "po-create" when parent only has "pricing-*" — not fine
            if not _is_sub_glob(tool, parent_scope.allowed_tools):
                raise AttenuationError(
                    f"Tool '{tool}' is not permitted by parent scope. "
                    f"Parent allows: {list(parent_scope.allowed_tools)}"
                )

    # --- Amount: child cap must be <= parent cap ---
    if max_amount is _UNSET:
        child_amount = parent_scope.max_amount
    else:
        child_amount = max_amount
    if parent_scope.max_amount is not None and child_amount is not None:
        if child_amount > parent_scope.max_amount:
            raise AttenuationError(
                f"Child max_amount ({child_amount}) exceeds parent ({parent_scope.max_amount})"
            )
    elif parent_scope.max_amount is not None and child_amount is None:
        # Parent has a cap, child tries to remove it — not allowed
        raise AttenuationError("Cannot remove amount cap that parent scope enforces")

    # --- Regions: child regions must be a subset of parent regions ---
    child_regions = allowed_regions if allowed_regions is not None else parent_scope.allowed_regions
    if parent_scope.allowed_regions:
        for region in child_regions:
            if region not in parent_scope.allowed_regions:
                raise AttenuationError(
                    f"Region '{region}' not in parent scope: {list(parent_scope.allowed_regions)}"
                )

    # --- Read-only: can escalate to read_only, cannot de-escalate ---
    child_read_only = read_only if read_only is not None else parent_scope.read_only
    if parent_scope.read_only and not child_read_only:
        raise AttenuationError("Cannot remove read_only constraint from parent scope")

    # --- Custom constraints: child must include all parent constraints ---
    parent_constraints = dict(parent_scope.custom_constraints)
    child_constraints_dict = dict(
        custom_constraints if custom_constraints is not None else parent_scope.custom_constraints
    )
    for key, value in parent_constraints.items():
        if key not in child_constraints_dict:
            raise AttenuationError(
                f"Parent custom constraint '{key}={value}' missing from child scope"
            )

    return DelegationScope(
        allowed_tools=child_tools,
        max_amount=child_amount,
        allowed_regions=child_regions,
        read_only=child_read_only,
        custom_constraints=tuple(child_constraints_dict.items()),
    )


def _is_sub_glob(child_pattern: str, parent_patterns: tuple[str, ...]) -> bool:
    """Check if a child glob pattern is logically a subset of any parent pattern.

    Simple heuristic: if the child is a concrete name (no wildcards) and matches
    a parent glob, it's a subset. If the child is itself a glob, it must exactly
    match a parent glob or be a more specific prefix.
    """
    for parent in parent_patterns:
        if fnmatch(child_pattern, parent):
            return True
        # e.g., child="pricing-api*" parent="pricing-*" — child is narrower
        if "*" in child_pattern and "*" in parent:
            child_prefix = child_pattern.split("*")[0]
            parent_prefix = parent.split("*")[0]
            if child_prefix.startswith(parent_prefix) and len(child_prefix) >= len(parent_prefix):
                return True
    return False
