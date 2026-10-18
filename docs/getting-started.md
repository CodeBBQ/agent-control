# Run the local dashboard

This source inspection copy requires Python 3.11+ and its `pyproject.toml` dependencies. In an isolated environment install the project, run `agent-control config` in an interactive terminal, then use `agent-control start` to launch the managed local dashboard.

The configuration selects an explicit list of GitHub repositories and an optional owner login. GitHub CLI authentication may be used for private repository viewing. Never put credentials into committed source or issue descriptions.

See [features](features.md), [operator guide](operations.md), [architecture](architecture/overview.md) and [security](security.md). CI checks and source availability are not evidence that an actual macOS service or private remote route has passed acceptance.

The GitHub patch importer, machine-readable agent-event publishing and agent-rule synchronization are not included in this publication-oriented copy.
