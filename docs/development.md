# Development and quality checks

Contributor and agent engineering rules are authoritative in
[AGENTS.md](../AGENTS.md). Check the current owning issue, branch, PR and
exact HEAD before work; preserve human integration approval gates.

## Development checks

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy agent_control
.venv/bin/python -m pytest -q
```

The CI workflow uses GitHub-hosted Python 3.11 and 3.12 and read-only repository permissions. Its existence is not evidence that a particular revision passed; inspect the check suite for the exact PR head.


Changes to source code should carry suitable tests, changelog entries and
documentation with the same scoped change. A passing earlier CI run is not
evidence that the new head was verified.

See [architecture](architecture/overview.md), [security](security.md) and
[the changelog](../CHANGELOG.md).
