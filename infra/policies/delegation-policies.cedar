// Coordinator agents can delegate to specialist agents
// but only if chain depth stays under 3 hops
permit(
    principal is AgenticDelegation::Agent,
    action == AgenticDelegation::Action::"delegateTask",
    resource is AgenticDelegation::Agent
) when {
    principal.agentType == "coordinator" &&
    resource.agentType == "specialist" &&
    context.chainDepth < 3
};

// Specialist agents can call financial tools
// only if delegation chain grants it AND amount is within delegated cap
permit(
    principal is AgenticDelegation::Agent,
    action == AgenticDelegation::Action::"callTool",
    resource is AgenticDelegation::Tool
) when {
    principal.agentType == "specialist" &&
    resource.category == "financial" &&
    context.maxAmount > 0 &&
    context.chainDepth >= 1
};

// Specialist agents can call read-only tools
permit(
    principal is AgenticDelegation::Agent,
    action == AgenticDelegation::Action::"callTool",
    resource is AgenticDelegation::Tool
) when {
    principal.agentType == "specialist" &&
    (resource.category == "data-read" || resource.category == "external") &&
    context.chainDepth >= 1
};

// Deny any tool call if delegation chain exceeds max depth
forbid(
    principal is AgenticDelegation::Agent,
    action == AgenticDelegation::Action::"callTool",
    resource is AgenticDelegation::Tool
) when {
    context.chainDepth > 4
};

// Read-only delegations cannot invoke write tools
forbid(
    principal is AgenticDelegation::Agent,
    action == AgenticDelegation::Action::"callTool",
    resource is AgenticDelegation::Tool
) when {
    context.readOnly == true &&
    resource.category == "data-write"
};
