# Operator guide

## Identities and files

- Application: `agent-control-service`
- Default endpoint: `http://127.0.0.1:8420`
- LaunchAgent: `com.codebbq.agent-control-service`
- Config: `~/.config/agent-control-service/config.toml` by default; use `agent-control config` in an interactive terminal to create/edit. State/cache use absolute paths below the user's home; optional `--config` overrides remain.
- Suggested private Serve port: HTTPS `8443`, never implicit HTTPS `443`

The service does not inspect or stop unrelated applications.

## Foreground and background

Use `serve` for foreground development. `start` writes a managed PID record only for a process launched with the fixed module/config/host/port marker. `status` separately reports listener ownership and HTTP health. `stop` refuses ambiguous/unowned processes and never sends `SIGKILL`.

`install-launch-agent` is macOS-only and replaces only a plist carrying the exact fixed label at the fixed user path. It bootstraps the current user domain. Log paths are under the configured cache directory.

## Tailscale

`enable-private-serve` first parses `tailscale serve status --json`, checks all relevant service/TCP/Web/Funnel ownership for the selected port, refuses port 443, verifies Tailscale is connected, adds one dedicated HTTPS proxy, and rereads status for an exact mapping. It never resets Serve or disables another route.

There is intentionally no automatic disable command: removal should be an operator-observed action until real host behavior is accepted. Check existing operator-owned routes and tailnet ACLs before enabling.

## Manual acceptance checklist

- Start/status/stop on the actual macOS account and confirm unowned listener refusal.
- Install/reinstall LaunchAgent, log out/in or restart, and confirm exact config/process/log identity.
- Confirm GitHub read permissions against each real private allowlisted repository.
- Confirm current-head checks and stale/offline display in the actual account.
- Inspect browser desktop and narrow/mobile layouts with real private external text.
- Review Tailscale ACLs, add the dedicated route, confirm no Funnel, preserve existing port 443 mapping, and test from the physical phone.
- Confirm the expected limitation: central remote state can persist, but this local site is unavailable while the Mac sleeps.

## First-run configuration

Run `agent-control start` or `agent-control config` from a terminal. The numbered setup menu collects only explicit allowlisted repositories and safe local choices, storing a private TOML config without tokens. For private repositories, use existing `gh auth login` credentials; `gh auth login` is available as a menu action, never invoked by the background process. Once saved, all regular commands find the config automatically; login/LaunchAgent runs must be provisioned first because stdin is not interactive. For restricted viewer permissions choose the legacy named read-only environment token instead of a broadly scoped `gh` credential. This distribution has no machine-readable issue-event publisher.
