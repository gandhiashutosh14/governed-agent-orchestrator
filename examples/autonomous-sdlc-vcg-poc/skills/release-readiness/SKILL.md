---
name: release-readiness
description: Verify commit-bound release gates and request required human approvals.
minimum_tier: frontier-agent
---
# Release readiness
Verify CI for the exact head SHA, valid independent reviews, policy and DoD checks, context freshness and human authorization. The agent may propose a release, but may not approve or deploy autonomously. Invalidate prior receipts when code, spec or permission revision changes.