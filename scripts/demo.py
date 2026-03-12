#!/usr/bin/env python3
"""Local demo — runs the full procurement delegation flow without AWS.

This script exercises the complete delegation chain pattern:
  User → Coordinator → Pricing Agent (read-only)
  User → Coordinator → Purchasing Agent (amount-capped)

It shows exactly what happens at each hop: token minting, scope
attenuation, validation, tool invocation checks, and audit logging.

Usage:
    python scripts/demo.py
"""

import copy
import json
import sys
import os

# Add repo root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.delegation.models import DelegationChain, DelegationScope
from lib.delegation.mint import mint_delegation
from lib.delegation.validate import validate_delegation, ValidationError
from lib.delegation.attenuate import attenuate_scope, AttenuationError
from lib.delegation.audit import DelegationAuditor


def _header(text: str) -> None:
    print(f"\n{'='*70}")
    print(f"  {text}")
    print(f"{'='*70}\n")


def _step(text: str) -> None:
    print(f"  → {text}")


def _result(label: str, data: object) -> None:
    if isinstance(data, dict):
        print(f"    {label}: {json.dumps(data, indent=6, default=str)}")
    else:
        print(f"    {label}: {data}")


def run_demo():
    """Run the full procurement delegation scenario."""

    _header("SECURE MULTI-AGENT DELEGATION CHAINS — LOCAL DEMO")
    print("  Scenario: procurement-manager@acme.com wants to find the best")
    print("  price for 500 units of SKU-4821 and place a PO if under $10,000.\n")

    # ── Step 1: User → Coordinator ─────────────────────────────────
    _header("Step 1: User grants authority to Coordinator Agent")

    chain = DelegationChain(originating_user="procurement-manager@acme.com")
    root_scope = DelegationScope(
        allowed_tools=("pricing-api*", "inventory-read*", "po-create*"),
        max_amount=10000.0,
        allowed_regions=("us-east-1",),
    )
    chain, root_token = mint_delegation(
        chain,
        parent_agent_id="user",
        child_agent_id="coordinator",
        child_scope=root_scope,
    )

    coordinator_auditor = DelegationAuditor("coordinator")
    coordinator_auditor.log_delegation_mint(chain, "user", "coordinator")

    _step(f"Root token minted: {root_token.token_id[:8]}...")
    _step(f"Scope: tools={list(root_scope.allowed_tools)}, max_amount=${root_scope.max_amount}")
    _step(f"Chain depth: {chain.depth}")

    # ── Step 2: Coordinator → Pricing Agent (read-only) ────────────
    _header("Step 2: Coordinator delegates to Pricing Agent (read-only)")

    pricing_chain = copy.deepcopy(chain)
    pricing_scope = DelegationScope(
        allowed_tools=("pricing-api*", "inventory-read*"),
        max_amount=10000.0,
        read_only=True,
        allowed_regions=("us-east-1",),
    )
    pricing_chain, pricing_token = mint_delegation(
        pricing_chain,
        parent_agent_id="coordinator",
        child_agent_id="pricing-agent",
        child_scope=pricing_scope,
    )

    coordinator_auditor.log_delegation_mint(pricing_chain, "coordinator", "pricing-agent")

    _step(f"Pricing token minted: {pricing_token.token_id[:8]}...")
    _step(f"Scope: tools={list(pricing_scope.allowed_tools)}, read_only=True")
    _step(f"Chain depth: {pricing_chain.depth}")
    _step(f"Chain hash links to previous hop: {pricing_token.chain_hash[:16]}...")

    # ── Step 3: Pricing Agent validates and acts ───────────────────
    _header("Step 3: Pricing Agent validates delegation and invokes tools")

    pricing_auditor = DelegationAuditor("pricing-agent")

    token = validate_delegation(pricing_chain, "pricing-agent")
    _step(f"Delegation validated for: {token.child_agent_id}")
    _step(f"Chain integrity: {'VALID' if pricing_chain.verify_integrity() else 'BROKEN'}")

    scope = pricing_chain.current_scope()

    # Tool: pricing-api__get_quotes — ALLOWED
    tool_name = "pricing-api__get_quotes"
    tool_input = {"sku": "SKU-4821", "quantity": 500}
    permitted = scope.tool_permitted(tool_name)
    decision = "ALLOW" if permitted else "DENY"
    pricing_auditor.log_tool_invocation(
        pricing_chain, tool_name, tool_input, decision,
        policy_decisions=[{"policyId": "policy-003", "effect": "PERMIT", "reason": "read-only specialist, tool in scope"}],
    )
    _step(f"Tool: {tool_name} → {decision}")
    if permitted:
        _result("Mock result", {"sku": "SKU-4821", "quantity": 500, "unit_price": 14.45, "total": 7225.0, "supplier": "Acme Corp"})

    # Tool: pricing-api__get_history — ALLOWED
    tool_name = "pricing-api__get_history"
    tool_input = {"sku": "SKU-4821"}
    permitted = scope.tool_permitted(tool_name)
    decision = "ALLOW" if permitted else "DENY"
    pricing_auditor.log_tool_invocation(
        pricing_chain, tool_name, tool_input, decision,
    )
    _step(f"Tool: {tool_name} → {decision}")

    # Tool: po-create — DENIED (not in scope)
    tool_name = "po-create__create_po"
    tool_input = {"sku": "SKU-4821", "quantity": 500, "amount": 7225.0}
    permitted = scope.tool_permitted(tool_name)
    decision = "ALLOW" if permitted else "DENY"
    pricing_auditor.log_tool_invocation(
        pricing_chain, tool_name, tool_input, decision,
        reason="Tool not in delegated scope",
    )
    _step(f"Tool: {tool_name} → {decision} (not in scope)")

    # ── Step 4: Coordinator → Purchasing Agent (amount-capped) ─────
    _header("Step 4: Coordinator delegates to Purchasing Agent (amount-capped)")

    purchasing_chain = copy.deepcopy(chain)
    purchasing_scope = DelegationScope(
        allowed_tools=("po-create*",),
        max_amount=10000.0,
        allowed_regions=("us-east-1",),
    )
    purchasing_chain, purchasing_token = mint_delegation(
        purchasing_chain,
        parent_agent_id="coordinator",
        child_agent_id="purchasing-agent",
        child_scope=purchasing_scope,
    )

    coordinator_auditor.log_delegation_mint(purchasing_chain, "coordinator", "purchasing-agent")

    _step(f"Purchasing token minted: {purchasing_token.token_id[:8]}...")
    _step(f"Scope: tools={list(purchasing_scope.allowed_tools)}, max_amount=${purchasing_scope.max_amount}")

    # ── Step 5: Purchasing Agent validates and acts ─────────────────
    _header("Step 5: Purchasing Agent validates and creates PO")

    purchasing_auditor = DelegationAuditor("purchasing-agent")

    token = validate_delegation(purchasing_chain, "purchasing-agent")
    _step(f"Delegation validated for: {token.child_agent_id}")

    scope = purchasing_chain.current_scope()

    # PO at $7,225 — ALLOWED (under $10k cap)
    tool_name = "po-create__create_po"
    tool_input = {"sku": "SKU-4821", "quantity": 500, "amount": 7225.0}
    tool_ok = scope.tool_permitted(tool_name)
    amount_ok = scope.amount_permitted(tool_input["amount"])
    decision = "ALLOW" if (tool_ok and amount_ok) else "DENY"
    purchasing_auditor.log_tool_invocation(
        purchasing_chain, tool_name, tool_input, decision,
        policy_decisions=[
            {"policyId": "policy-002", "effect": "PERMIT", "reason": "specialist + financial + under cap"},
            {"policyId": "policy-003", "effect": "NOT_APPLICABLE", "reason": "chainDepth=2, under limit"},
        ],
    )
    _step(f"Tool: {tool_name} (amount=$7,225) → {decision}")
    _result("Mock PO", {"po_id": "PO-A1B2C3D4", "sku": "SKU-4821", "quantity": 500, "amount": 7225.0, "status": "CREATED"})

    # PO at $12,000 — DENIED (over cap)
    tool_input_over = {"sku": "SKU-4821", "quantity": 700, "amount": 12000.0}
    amount_ok = scope.amount_permitted(tool_input_over["amount"])
    decision = "ALLOW" if (tool_ok and amount_ok) else "DENY"
    purchasing_auditor.log_tool_invocation(
        purchasing_chain, tool_name, tool_input_over, decision,
        reason=f"Amount ${tool_input_over['amount']} exceeds cap ${scope.max_amount}",
    )
    _step(f"Tool: {tool_name} (amount=$12,000) → {decision} (exceeds $10k cap)")

    # Pricing tool — DENIED (not in scope)
    tool_name = "pricing-api__get_quotes"
    tool_input = {"sku": "SKU-4821"}
    permitted = scope.tool_permitted(tool_name)
    decision = "ALLOW" if permitted else "DENY"
    purchasing_auditor.log_tool_invocation(
        purchasing_chain, tool_name, tool_input, decision,
        reason="Tool not in delegated scope",
    )
    _step(f"Tool: {tool_name} → {decision} (not in scope)")

    # ── Step 6: Demonstrate attenuation enforcement ────────────────
    _header("Step 6: Scope escalation attempt (should fail)")

    try:
        escalated_scope = DelegationScope(
            allowed_tools=("*",),
            max_amount=50000.0,
        )
        mint_delegation(
            copy.deepcopy(chain),
            parent_agent_id="coordinator",
            child_agent_id="rogue-agent",
            child_scope=escalated_scope,
        )
        _step("ERROR: escalation should have been blocked")
    except AttenuationError as e:
        _step(f"Blocked: {e}")

    # ── Step 7: Tamper detection ───────────────────────────────────
    _header("Step 7: Chain tamper detection")

    tampered = copy.deepcopy(pricing_chain)
    tampered.hops[0] = tampered.hops[0].__class__(
        parent_agent_id="user",
        child_agent_id="coordinator",
        scope=DelegationScope(allowed_tools=("*",), max_amount=999999.0),
    )
    integrity = tampered.verify_integrity()
    _step(f"Tampered chain integrity: {'VALID' if integrity else 'BROKEN (tampering detected)'}")

    try:
        validate_delegation(tampered, "pricing-agent")
        _step("ERROR: tampered chain should have been rejected")
    except ValidationError as e:
        _step(f"Validation rejected: {e}")

    # ── Step 8: Cedar context output ───────────────────────────────
    _header("Step 8: Cedar policy evaluation context")

    _step("This is what gets sent to AgentCore Policy Engine:")
    ctx = purchasing_chain.to_cedar_context()
    print(json.dumps(ctx, indent=4, default=str))

    # ── Summary ────────────────────────────────────────────────────
    _header("DEMO COMPLETE")
    print("  What you just saw:")
    print("  - Delegation tokens minted with narrowing scopes at each hop")
    print("  - Read-only agent blocked from write tools")
    print("  - Amount-capped agent blocked from exceeding $10k")
    print("  - Scope escalation attempt blocked by attenuation rules")
    print("  - Chain tampering detected via hash verification")
    print("  - Structured audit events emitted for every decision")
    print("  - Cedar context generated for policy engine evaluation")
    print()
    print("  To deploy on AWS: cd infra && npm install && npx cdk deploy")
    print()


if __name__ == "__main__":
    run_demo()
