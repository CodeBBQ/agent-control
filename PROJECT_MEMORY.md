# Project memory — public dashboard copy

The source repository is `CodeBBQ/agent-control`. This is a **publication-oriented version** of a local GitHub dashboard; it does not include the private machine-event publisher, rule-synchronization feature or patch-import mechanism.

## Verified architecture boundaries
- Ordinary GitHub issues are observed as workstreams without registering hidden markers.
- GitHub remains authoritative for issue, milestone, branch, pull-request, review and CI facts.
- Exact current-HEAD CI is distinguished from older checks. Stale and unavailable observations are labeled.
- Cached snapshots serve the web UI; live GitHub fetches occur outside normal page rendering.
- Local demo/test JSON state is separate from read-only GitHub observation.
- Agent runtime/liveness is not inferred from commits, comments or PRs.
- Operator-initiated service-update and PR-ready/merge behavior is a separate security-sensitive function. These actions require explicit authorization and current-head checks; successful real macOS/remote acceptance must not be inferred from tests.

## Scope and validation
Source code, documentation and CI remain the source for actual supported features. Real GitHub permissions, macOS managed service behavior, Tailscale routes, browser layout and owner workflows require validation in their respective environments.

This document is a high-level boundary summary, not an executable collaboration protocol or a claim of acceptance.
