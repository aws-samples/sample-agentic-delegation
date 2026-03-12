import * as cdk from "aws-cdk-lib";
import * as logs from "aws-cdk-lib/aws-logs";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as iam from "aws-cdk-lib/aws-iam";
import { Construct } from "constructs";
import {
  Runtime as AgentCoreRuntime,
  AgentRuntimeArtifact,
  Gateway,
  GatewayAuthorizer,
  McpProtocolConfiguration,
  McpGatewaySearchType,
  ToolSchema,
  ToolDefinition,
  SchemaDefinitionType,
} from "@aws-cdk/aws-bedrock-agentcore-alpha";
import { NagSuppressions } from "cdk-nag";
import { DelegationAuditDashboard } from "./delegation-audit-dashboard";

export class DelegationMeshStack extends cdk.Stack {
  /** AgentCore Runtime for the Coordinator agent */
  public readonly coordinatorRuntime: AgentCoreRuntime;
  /** AgentCore Runtime for the Pricing specialist agent */
  public readonly pricingRuntime: AgentCoreRuntime;
  /** AgentCore Runtime for the Purchasing specialist agent */
  public readonly purchasingRuntime: AgentCoreRuntime;
  /** AgentCore Gateway with MCP tool targets */
  public readonly gateway: Gateway;
  /** CloudWatch log group for delegation chain audit */
  public readonly auditLogGroup: logs.LogGroup;
  /** CloudWatch dashboard for delegation chain monitoring */
  public readonly auditDashboard: DelegationAuditDashboard;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // ── Audit Log Group ───────────────────────────────────────────────
    this.auditLogGroup = new logs.LogGroup(this, "DelegationAuditLog", {
      logGroupName: "/agentic-delegation/audit",
      retention: logs.RetentionDays.SIX_MONTHS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ── Gateway (MCP tool endpoint with policy enforcement) ───────────
    this.gateway = new Gateway(this, "ProcurementGateway", {
      gatewayName: "procurement-gateway",
      description: "MCP gateway for procurement tools with Cedar policy enforcement",
      protocolConfiguration: new McpProtocolConfiguration({
        instructions:
          "Procurement tools gateway. All tool calls are subject to Cedar delegation chain policies.",
        searchType: McpGatewaySearchType.SEMANTIC,
      }),
      authorizerConfiguration: GatewayAuthorizer.usingAwsIam(),
    });

    // ── Lambda: Pricing API tool ──────────────────────────────────────
    const pricingLambda = new lambda.Function(this, "PricingApiFunction", {
      functionName: "procurement-pricing-api",
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "index.handler",
      code: lambda.Code.fromInline(`
import json

MOCK_PRICES = {
    "SKU-4821": {"unit_price": 17.00, "bulk_discount": 0.15, "supplier": "Acme Corp"},
    "SKU-1234": {"unit_price": 42.50, "bulk_discount": 0.10, "supplier": "GlobalParts"},
}

def handler(event, context):
    tool_name = event.get("name", "")
    args = event.get("arguments", {})

    if "get_quotes" in tool_name or "get-quotes" in tool_name:
        sku = args.get("sku", "UNKNOWN")
        quantity = args.get("quantity", 1)
        price_info = MOCK_PRICES.get(sku, {"unit_price": 0, "bulk_discount": 0, "supplier": "Unknown"})
        unit = price_info["unit_price"] * (1 - price_info["bulk_discount"]) if quantity >= 100 else price_info["unit_price"]
        return {"sku": sku, "quantity": quantity, "unit_price": unit, "total": unit * quantity, "supplier": price_info["supplier"]}

    if "get_history" in tool_name or "get-history" in tool_name:
        sku = args.get("sku", "UNKNOWN")
        return {"sku": sku, "price_history": [{"date": "2026-01-15", "price": 18.50}, {"date": "2026-02-15", "price": 17.00}]}

    return {"error": f"Unknown tool: {tool_name}"}
`),
      timeout: cdk.Duration.seconds(30),
    });

    // ── Lambda: Inventory Read tool ───────────────────────────────────
    const inventoryLambda = new lambda.Function(this, "InventoryReadFunction", {
      functionName: "procurement-inventory-read",
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "index.handler",
      code: lambda.Code.fromInline(`
import json

MOCK_INVENTORY = {
    "SKU-4821": {"available": 2500, "warehouse": "us-east-1", "reorder_point": 500},
    "SKU-1234": {"available": 150, "warehouse": "eu-west-1", "reorder_point": 50},
}

def handler(event, context):
    args = event.get("arguments", {})
    sku = args.get("sku", "UNKNOWN")
    inv = MOCK_INVENTORY.get(sku, {"available": 0, "warehouse": "unknown", "reorder_point": 0})
    return {"sku": sku, **inv}
`),
      timeout: cdk.Duration.seconds(30),
    });

    // ── Lambda: PO Create tool ────────────────────────────────────────
    const poCreateLambda = new lambda.Function(this, "PoCreateFunction", {
      functionName: "procurement-po-create",
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "index.handler",
      code: lambda.Code.fromInline(`
import json, uuid
from datetime import datetime

def handler(event, context):
    args = event.get("arguments", {})
    sku = args.get("sku", "UNKNOWN")
    quantity = args.get("quantity", 0)
    amount = args.get("amount", 0)
    return {
        "po_id": f"PO-{uuid.uuid4().hex[:8].upper()}",
        "sku": sku,
        "quantity": quantity,
        "amount": amount,
        "status": "CREATED",
        "created_at": datetime.utcnow().isoformat(),
    }
`),
      timeout: cdk.Duration.seconds(30),
    });

    // ── Gateway Targets ───────────────────────────────────────────────
    const pricingToolSchema = ToolSchema.fromInline([
      {
        name: "get_quotes",
        description: "Get price quotes for a SKU with quantity-based discounts",
        inputSchema: {
          type: SchemaDefinitionType.OBJECT,
          properties: {
            sku: { type: SchemaDefinitionType.STRING, description: "Product SKU" },
            quantity: { type: SchemaDefinitionType.NUMBER, description: "Order quantity" },
          },
          required: ["sku", "quantity"],
        },
      } as ToolDefinition,
      {
        name: "get_history",
        description: "Get historical pricing data for a SKU",
        inputSchema: {
          type: SchemaDefinitionType.OBJECT,
          properties: {
            sku: { type: SchemaDefinitionType.STRING, description: "Product SKU" },
          },
          required: ["sku"],
        },
      } as ToolDefinition,
    ]);

    this.gateway.addLambdaTarget("PricingTarget", {
      gatewayTargetName: "pricing-api",
      description: "Pricing API — read-only price quotes and history",
      lambdaFunction: pricingLambda,
      toolSchema: pricingToolSchema,
    });

    this.gateway.addLambdaTarget("InventoryTarget", {
      gatewayTargetName: "inventory-read",
      description: "Inventory read — check stock levels",
      lambdaFunction: inventoryLambda,
      toolSchema: ToolSchema.fromInline([
        {
          name: "check_inventory",
          description: "Check available inventory for a SKU",
          inputSchema: {
            type: SchemaDefinitionType.OBJECT,
            properties: {
              sku: { type: SchemaDefinitionType.STRING, description: "Product SKU" },
            },
            required: ["sku"],
          },
        } as ToolDefinition,
      ]),
    });

    this.gateway.addLambdaTarget("PoCreateTarget", {
      gatewayTargetName: "po-create",
      description: "Purchase Order creation — write operation, subject to amount caps",
      lambdaFunction: poCreateLambda,
      toolSchema: ToolSchema.fromInline([
        {
          name: "create_po",
          description: "Create a purchase order for a SKU",
          inputSchema: {
            type: SchemaDefinitionType.OBJECT,
            properties: {
              sku: { type: SchemaDefinitionType.STRING, description: "Product SKU" },
              quantity: { type: SchemaDefinitionType.NUMBER, description: "Order quantity" },
              amount: { type: SchemaDefinitionType.NUMBER, description: "Total PO amount in USD" },
            },
            required: ["sku", "quantity", "amount"],
          },
        } as ToolDefinition,
      ]),
    });

    // ── Agent Runtimes (placeholder artifacts — replaced when agents are built) ──
    // Using fromCodeAsset with placeholder code. In Tasks 3 & 4, these will be
    // replaced with actual agent implementations.

    this.coordinatorRuntime = new AgentCoreRuntime(this, "CoordinatorRuntime", {
      runtimeName: "coordinator_agent",
      description: "Coordinator agent — decomposes tasks and delegates to specialists with attenuated scopes",
      agentRuntimeArtifact: AgentRuntimeArtifact.fromAsset("../agents/coordinator"),
    });

    this.pricingRuntime = new AgentCoreRuntime(this, "PricingRuntime", {
      runtimeName: "pricing_agent",
      description: "Pricing specialist — read-only access to pricing and inventory tools",
      agentRuntimeArtifact: AgentRuntimeArtifact.fromAsset("../agents/pricing"),
    });

    this.purchasingRuntime = new AgentCoreRuntime(this, "PurchasingRuntime", {
      runtimeName: "purchasing_agent",
      description: "Purchasing specialist — write access to PO creation, capped by delegated amount",
      agentRuntimeArtifact: AgentRuntimeArtifact.fromAsset("../agents/purchasing"),
    });

    // Grant runtimes permission to invoke the gateway
    this.gateway.grantInvoke(this.coordinatorRuntime);
    this.gateway.grantInvoke(this.pricingRuntime);
    this.gateway.grantInvoke(this.purchasingRuntime);

    // Grant coordinator permission to invoke specialist runtimes (A2A delegation)
    this.pricingRuntime.grantInvoke(this.coordinatorRuntime);
    this.purchasingRuntime.grantInvoke(this.coordinatorRuntime);

    // ── Audit Dashboard ─────────────────────────────────────────────
    this.auditDashboard = new DelegationAuditDashboard(
      this,
      "AuditDashboard",
      { logGroup: this.auditLogGroup },
    );

    // ── Outputs ───────────────────────────────────────────────────────
    new cdk.CfnOutput(this, "GatewayUrl", {
      value: this.gateway.gatewayUrl ?? "PENDING",
      description: "AgentCore Gateway MCP endpoint URL",
    });

    new cdk.CfnOutput(this, "AuditLogGroupName", {
      value: this.auditLogGroup.logGroupName,
      description: "CloudWatch log group for delegation chain audit",
    });

    new cdk.CfnOutput(this, "DashboardUrl", {
      value: `https://${this.region}.console.aws.amazon.com/cloudwatch/home?region=${this.region}#dashboards:name=DelegationChainAudit`,
      description: "CloudWatch dashboard for delegation chain monitoring",
    });

    // ── cdk-nag suppressions (sample/demo code) ───────────────────────
    const sampleReason =
      "This is a reference architecture sample with mock data. " +
      "Production deployments should address these findings.";

    NagSuppressions.addStackSuppressions(this, [
      { id: "AwsSolutions-IAM4", reason: sampleReason },
      { id: "AwsSolutions-IAM5", reason: sampleReason },
      { id: "AwsSolutions-L1", reason: "Python 3.12 is the latest supported runtime for inline Lambda code in this sample." },
      { id: "AwsSolutions-CW2", reason: "Log group encryption not required for sample/demo audit data." },
    ]);

    NagSuppressions.addResourceSuppressions(
      [pricingLambda, inventoryLambda, poCreateLambda],
      [
        { id: "AwsSolutions-SQS3", reason: "DLQ not required for mock Lambda tools in a sample." },
        { id: "AwsSolutions-SQS4", reason: "DLQ not required for mock Lambda tools in a sample." },
      ],
      true,
    );
  }
}
