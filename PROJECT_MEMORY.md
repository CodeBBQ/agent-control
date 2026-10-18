# Project overview

Agent Control is a local-first FastAPI dashboard for an explicitly configured list of GitHub projects.

- It displays issues, milestones, workstreams, branches, pull requests, reviews and CI observations.
- Background GitHub refresh and cached snapshots keep page navigation independent of network latency.
- GitHub remains the source for GitHub facts. Old results and retrieval failures must be labeled.
- Local synthetic fixtures are distinct from connected repository observations.
- Optional local operator functions are permission-sensitive and must be evaluated separately from passive viewing.
- Runtime presence of external tools or agents is not established by GitHub activity.

Current capabilities and limitations are described in the code, tests and [README.md](README.md). Real-host, authentication and mobile-browser checks require their own acceptance evidence.
