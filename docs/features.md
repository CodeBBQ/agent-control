# Features — public dashboard copy

The local FastAPI/Jinja dashboard visualizes GitHub issues and milestones, candidate workstreams, recent pull requests, exact-head CI/review status and associated navigation. It can retain the last successful snapshot while explicitly marking stale evidence or retrieval failures.

No hidden issue marker is required. No machine-readable issue event publisher, agent-event CLI or repository-wide rule synchronizer is shipped. A normal GitHub comment cannot establish live agent execution or owner acceptance.

Local operator features include configurable allowlists and separately guarded service/PR operations. The scope of an authenticated GitHub CLI credential can exceed what this application exercises. Security checks and live-host acceptance are distinct.

Synthetic fixtures and hosted CI cannot establish real macOS, Tailscale, private GitHub or mobile-browser acceptance. See [security](security.md) and [operations](operations.md).
