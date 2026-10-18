# Architecture overview

This copy implements a local-first GitHub observation dashboard. A bounded background GitHub adapter reads the configured repository allowlist; a conservative projection associates ordinary issues, PR branches, milestone data and current-HEAD CI/reviews. Cached results are served by FastAPI/Jinja without synchronously re-fetching GitHub for each page.

Local demo data is deliberately separate from live GitHub facts. Missing and stale evidence must not be presented as verified status, and GitHub activity is not proof of a running agent.

The public copy does not expose a machine-readable issue-event protocol or cross-repository rule publishing mechanism.
