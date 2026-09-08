---
name: github-actions
description: Maintain cdr-energy-research's GitHub Actions callers and existing validation without changing its application or deployment contract.
---

# Maintain repository automation

Read [project automation](../../../docs/shared-automation.md), the current workflow and repository instructions before editing.
Use the [pinned shared workflow contract](https://github.com/Artic0din/reusable-workflows/blob/c913ad3e22a42c75bfcf0029448cda48dc546ff1/docs/workflow-contracts.md) for supported inputs and permissions.
Research tooling and snapshot datasets for Australian energy product APIs.

## Implement

Keep each existing application check, event, runner requirement and protected check context.
Use read-only permissions and full commit pins with release comments for shared checks.
Keep Copilot setup, dependency ecosystems and application build commands local.
Do not enable CodeQL, dependency auto-merge, deployment or production writes unless separately authorised.
Use offline fixtures; do not run retailer sweeps, probes, catalogue publication or the real response cache to validate documentation or automation.

## Validate

Validate changed workflow YAML with actionlint and applicable repository checks.
Check selected skill metadata and local links; run git diff --check and staged/outgoing Gitleaks scans before publishing.
Use the commands documented in project automation for the affected application scope, with the repository's locked dependencies.
Inspect actual GitHub results and preserve failure propagation; parsing YAML does not prove runtime or live-service acceptance.
