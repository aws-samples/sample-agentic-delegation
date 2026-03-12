import * as cdk from "aws-cdk-lib";
import * as cw from "aws-cdk-lib/aws-cloudwatch";
import * as logs from "aws-cdk-lib/aws-logs";
import { Construct } from "constructs";

export interface DelegationAuditDashboardProps {
  logGroup: logs.ILogGroup;
}

/**
 * CloudWatch Dashboard for monitoring delegation chain activity.
 *
 * Provides widgets for:
 * - Tool invocation decisions (ALLOW vs DENY)
 * - Delegation chain depth distribution
 * - Deny rate over time
 * - Per-agent activity breakdown
 * - Near-cap tool calls (amount within 10% of limit)
 */
export class DelegationAuditDashboard extends Construct {
  public readonly dashboard: cw.Dashboard;

  constructor(
    scope: Construct,
    id: string,
    props: DelegationAuditDashboardProps,
  ) {
    super(scope, id);

    const logGroupName = props.logGroup.logGroupName;

    this.dashboard = new cw.Dashboard(this, "Dashboard", {
      dashboardName: "DelegationChainAudit",
      defaultInterval: cdk.Duration.hours(24),
    });

    // ── Row 1: Decision overview ──────────────────────────────────────

    const allowCount = new logs.MetricFilter(this, "AllowCount", {
      logGroup: props.logGroup,
      filterPattern: logs.FilterPattern.literal('{ $.decision = "ALLOW" }'),
      metricNamespace: "AgenticDelegation",
      metricName: "AllowedInvocations",
      metricValue: "1",
      defaultValue: 0,
    });

    const denyCount = new logs.MetricFilter(this, "DenyCount", {
      logGroup: props.logGroup,
      filterPattern: logs.FilterPattern.literal('{ $.decision = "DENY" }'),
      metricNamespace: "AgenticDelegation",
      metricName: "DeniedInvocations",
      metricValue: "1",
      defaultValue: 0,
    });

    const allowMetric = new cw.Metric({
      namespace: "AgenticDelegation",
      metricName: "AllowedInvocations",
      statistic: "Sum",
      period: cdk.Duration.minutes(5),
    });

    const denyMetric = new cw.Metric({
      namespace: "AgenticDelegation",
      metricName: "DeniedInvocations",
      statistic: "Sum",
      period: cdk.Duration.minutes(5),
    });

    this.dashboard.addWidgets(
      new cw.GraphWidget({
        title: "Tool Invocation Decisions",
        width: 12,
        height: 6,
        left: [allowMetric],
        right: [denyMetric],
        leftYAxis: { label: "Allowed", showUnits: false },
        rightYAxis: { label: "Denied", showUnits: false },
      }),
      new cw.LogQueryWidget({
        title: "Recent Denied Invocations",
        width: 12,
        height: 6,
        logGroupNames: [logGroupName],
        queryLines: [
          "fields @timestamp, agent, tool, @message",
          'filter decision = "DENY"',
          "sort @timestamp desc",
          "limit 20",
        ],
      }),
    );

    // ── Row 2: Chain depth & agent activity ───────────────────────────

    this.dashboard.addWidgets(
      new cw.LogQueryWidget({
        title: "Delegation Chain Depth Distribution",
        width: 8,
        height: 6,
        logGroupNames: [logGroupName],
        view: cw.LogQueryVisualizationType.BAR,
        queryLines: [
          "stats count(*) as invocations by chainDepth",
          "sort chainDepth asc",
        ],
      }),
      new cw.LogQueryWidget({
        title: "Invocations by Agent",
        width: 8,
        height: 6,
        logGroupNames: [logGroupName],
        view: cw.LogQueryVisualizationType.PIE,
        queryLines: [
          "stats count(*) as invocations by agent",
          "sort invocations desc",
        ],
      }),
      new cw.LogQueryWidget({
        title: "Invocations by Tool",
        width: 8,
        height: 6,
        logGroupNames: [logGroupName],
        view: cw.LogQueryVisualizationType.PIE,
        queryLines: [
          "stats count(*) as invocations by tool",
          "sort invocations desc",
        ],
      }),
    );

    // ── Row 3: Deny rate & near-cap warnings ──────────────────────────

    this.dashboard.addWidgets(
      new cw.GraphWidget({
        title: "Deny Rate (%)",
        width: 8,
        height: 6,
        left: [
          new cw.MathExpression({
            expression: "100 * denied / (allowed + denied)",
            usingMetrics: {
              allowed: allowMetric,
              denied: denyMetric,
            },
            period: cdk.Duration.minutes(15),
            label: "Deny Rate %",
          }),
        ],
      }),
      new cw.LogQueryWidget({
        title: "Near-Cap Tool Calls (amount > 90% of limit)",
        width: 8,
        height: 6,
        logGroupNames: [logGroupName],
        queryLines: [
          "fields @timestamp, agent, tool, toolInput.amount as amount, maxAmount",
          "filter toolInput.amount > (maxAmount * 0.9)",
          "sort @timestamp desc",
          "limit 20",
        ],
      }),
      new cw.LogQueryWidget({
        title: "Deep Chains (depth > 2)",
        width: 8,
        height: 6,
        logGroupNames: [logGroupName],
        queryLines: [
          "fields @timestamp, agent, tool, chainDepth",
          "filter chainDepth > 2",
          "sort chainDepth desc",
          "limit 20",
        ],
      }),
    );

    // ── Row 4: Full chain trace (for debugging) ───────────────────────

    this.dashboard.addWidgets(
      new cw.LogQueryWidget({
        title: "Full Delegation Chain Trace (latest 10)",
        width: 24,
        height: 8,
        logGroupNames: [logGroupName],
        queryLines: [
          "fields @timestamp, agent, tool, decision, delegationChain, policyDecisions",
          "sort @timestamp desc",
          "limit 10",
        ],
      }),
    );

    // ── Alarm: High deny rate ─────────────────────────────────────────

    new cw.Alarm(this, "HighDenyRateAlarm", {
      alarmName: "AgenticDelegation-HighDenyRate",
      alarmDescription:
        "More than 10 delegation denials in 5 minutes — possible misconfigured scope or attack",
      metric: denyMetric,
      threshold: 10,
      evaluationPeriods: 1,
      comparisonOperator: cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cw.TreatMissingData.NOT_BREACHING,
    });
  }
}
