# Development guidance

This repository contains a local-first GitHub dashboard. These are general contribution and safety guidelines, not a specification for coordinating external AI agents.

## Before changing the project

- Read the relevant source, tests and documentation. Check the current branch and repository HEAD.
- Keep changes scoped to the requested feature or defect; avoid unrelated refactoring.
- Preserve existing test coverage and make status or capability claims only when verified.

## Engineering boundaries

- GitHub owns its issues, pull requests, reviews, commits and check results. The dashboard reads and presents those records; it does not establish whether an external agent process is running.
- Treat remote text and links as untrusted. Validate inputs, escape rendered content and protect credentials and local files.
- Keep network access and repository selection explicit. Do not add unapproved external write operations or broaden permissions.
- Distinguish current-commit CI results from historical results. Surface stale or unavailable data rather than inventing a successful state.

## Validation and integration

- Run applicable Python lint, format, type and regression checks. Inspect the resulting changes for privacy and behavior.
- Automated tests do not replace acceptance on an actual macOS host, browser or authenticated GitHub account.
- Do not merge changes, close issues, delete branches or rewrite history without the owner's authorization.

For product usage see [README.md](README.md), [architecture](docs/architecture/overview.md) and [security](docs/security.md).
