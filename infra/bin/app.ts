#!/usr/bin/env node
import "source-map-support/register";
import * as cdk from "aws-cdk-lib";
import { AwsSolutionsChecks } from "cdk-nag";
import { DelegationMeshStack } from "../lib/delegation-mesh-stack";

const app = new cdk.App();

new DelegationMeshStack(app, "DelegationMeshStack", {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION ?? "us-east-1",
  },
  description:
    "Secure Multi-Agent Delegation Chains — Cedar-based permission attenuation for agentic systems on AWS",
});

cdk.Aspects.of(app).add(new AwsSolutionsChecks({ verbose: true }));
