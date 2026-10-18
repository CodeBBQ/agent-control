"""Explicit terminal-only repository selection from the authenticated GitHub CLI."""

from __future__ import annotations

import json
import re
import subprocess

from agent_control.config import ConfigurationError, ProjectConfig

PAGE_SIZE = 20
MAX_DISCOVERED = 500
MAX_SELECTED = 50


def _project(repository: str) -> ProjectConfig:
    return ProjectConfig(
        id=re.sub(r"[^a-z0-9._-]", "-", repository.lower().replace("/", "-")),
        repository=repository,
        display_name=repository.split("/", 1)[-1],
    )


def discover_repositories() -> tuple[str, ...]:
    """List names only, after an explicit owner menu choice; never read tokens."""
    try:
        result = subprocess.run(
            ["gh", "repo", "list", "--limit", str(MAX_DISCOVERED), "--json", "nameWithOwner"],
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ConfigurationError(
            "GitHub CLI unavailable or timed out; install gh and run 'gh auth login'"
        ) from error
    if result.returncode:
        raise ConfigurationError(
            "Cannot list GitHub repositories; check 'gh auth login' and permissions"
        )
    try:
        payload = json.loads(result.stdout)
    except ValueError as error:
        raise ConfigurationError("GitHub CLI returned an invalid repository list") from error
    if not isinstance(payload, list):
        raise ConfigurationError("GitHub CLI returned an invalid repository list")
    repositories: dict[str, str] = {}
    for item in payload[:MAX_DISCOVERED]:
        if not isinstance(item, dict):
            continue
        name = item.get("nameWithOwner")
        if not isinstance(name, str):
            continue
        try:
            validated = _project(name).repository
        except ValueError:
            continue
        repositories.setdefault(validated.casefold(), validated)
    return tuple(sorted(repositories.values(), key=str.casefold))


def choose_repositories(
    current: tuple[ProjectConfig, ...],
) -> tuple[ProjectConfig, ...] | None:
    """Toggle explicit selections; None means abandon the entire selection menu."""
    selected = {project.repository.casefold(): project for project in current}
    catalog: tuple[str, ...] = ()

    def refresh() -> None:
        nonlocal catalog
        try:
            catalog = discover_repositories()
            print(f"Found {len(catalog)} GitHub repositories (names only).")
            if len(catalog) == MAX_DISCOVERED:
                print("List limit reached; use 'a' to add further repositories.")
        except ConfigurationError as error:
            print(f"{error}. Use 'a' to add a repository manually.")
            catalog = ()

    refresh()
    query = ""
    page = 0
    while True:
        visible = tuple(name for name in catalog if query.casefold() in name.casefold())
        pages = max(1, (len(visible) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, pages - 1)
        listing = visible[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
        print(
            f"\nGitHub repositories — {len(selected)} selected"
            f" | page {page + 1}/{pages} | filter: {query or 'all'}"
        )
        for number, repository in enumerate(listing, 1):
            mark = "x" if repository.casefold() in selected else " "
            print(f"  {number:2}) [{mark}] {repository}")
        unseen = [p.repository for p in selected.values() if p.repository not in catalog]
        if unseen:
            print("  Other selected: " + ", ".join(sorted(unseen, key=str.casefold)))
        print(
            "Toggle number(s), comma-separated; n/p pages; / filter; "
            "a add manually; r refresh; d done; q cancel"
        )
        answer = input("Selection: ").strip().lower()
        if answer == "q":
            return None
        if answer == "d":
            if not selected:
                print("Select at least one repository before saving.")
                continue
            return tuple(sorted(selected.values(), key=lambda p: p.repository.casefold()))
        if answer == "n":
            page = min(page + 1, pages - 1)
        elif answer == "p":
            page = max(0, page - 1)
        elif answer == "/":
            query = input("Filter repositories (empty shows all): ").strip()
            page = 0
        elif answer == "r":
            refresh()
            page = 0
        elif answer == "a":
            entries = input("Add owner/repo (comma-separated): ").strip()
            for entry in (part.strip() for part in entries.split(",")):
                if not entry:
                    continue
                if "/" not in entry:
                    matches = [
                        name
                        for name in catalog
                        if name.rsplit("/", 1)[-1].casefold() == entry.casefold()
                    ]
                    if len(matches) != 1:
                        print(f"Use owner/repo for {entry!r} (no unique catalog match).")
                        continue
                    entry = matches[0]
                try:
                    project = _project(entry)
                except ValueError:
                    print(f"Invalid repository name: {entry!r}; use owner/repo.")
                    continue
                key = project.repository.casefold()
                if key not in selected and len(selected) >= MAX_SELECTED:
                    print(f"Cannot select more than {MAX_SELECTED} repositories.")
                    continue
                selected[key] = project
        elif answer:
            try:
                indices = [int(part.strip()) for part in answer.split(",")]
                if not indices or any(n < 1 or n > len(listing) for n in indices):
                    raise ValueError("number is outside the current page")
                if len(set(indices)) != len(indices):
                    raise ValueError("duplicate selection number")
            except ValueError:
                print("Enter listed numbers separated by commas, or a menu command.")
                continue
            for index in indices:
                name = listing[index - 1]
                key = name.casefold()
                if key in selected:
                    del selected[key]
                elif len(selected) >= MAX_SELECTED:
                    print(f"Cannot select more than {MAX_SELECTED} repositories.")
                else:
                    selected[key] = _project(name)
