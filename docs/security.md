# Security and trust boundaries

The dashboard observes only explicitly configured GitHub repositories. External issue titles, comments and URLs are untrusted; their size and rendering must be bounded, and unsupported links must not become arbitrary fetches. Credentials and private user data must never appear in repository state, browser output or logs.

The HTTP service binds to loopback by default. Optional private remote viewing is a separate operator decision: Tailscale exposure does not itself establish application authentication. Operator settings, managed updates and PR actions require strict local Origin/CSRF authorization and independent actual-host acceptance. No agent-authored text grants owner authority.

The public copy has **no machine event publisher and no cross-repository agent-rule synchronizer**. Ordinary GitHub state can provide retrospective evidence but does not prove current agent execution.

Automated CI covers only its tested code revision and environment. Confirm real GitHub permissions, macOS process ownership, Tailscale ACLs and mobile behavior separately.
