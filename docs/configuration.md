# Local configuration

Configure GitHub repository allowlists and the local owner login through `agent-control config` or the guarded local dashboard Settings page. See [getting started](getting-started.md).

The GitHub reader can use an already authenticated `gh` installation or explicitly configured read credentials; their actual scopes are determined by GitHub and require operator review. Tokens should not be committed or pasted into repository documents.

The publication-oriented copy does not provide agent-rule synchronization or an issue event publisher. Settings operations are separate from opt-in managed service update/PR actions and remain subject to loopback Origin/CSRF guards.
