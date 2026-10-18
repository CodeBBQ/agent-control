"""Explicit, scoped GitHub PR ready/merge operation for the local operator panel.

No implicit approval or merge: branch, PR number, and exact head must match
a fresh gh CLI read before executing either operation. No shell is involved.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any

from agent_control.self_update import (
    ACTIVE,
    SelfUpdater,
    UpdateError,
    checked_checkout,
    valid_branch,
)
from agent_control.self_update import LOCK as UPDATE_LOCK

REPOSITORY = "CodeBBQ/agent-control"
HEAD = re.compile(r"[0-9a-f]{40}\Z")
SAFE_CHECK_CONCLUSIONS = {"SUCCESS", "NEUTRAL", "SKIPPED"}


class PRActionError(RuntimeError):
    """Safe user-facing failure without CLI stdout/stderr or credentials."""


def gh(*args: str) -> str:
    try:
        result = subprocess.run(
            ["gh", "pr", *args, "--repo", REPOSITORY],
            capture_output=True,
            text=True,
            check=False,
            timeout=35,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "GH_PROMPT_DISABLED": "1", "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PRActionError("GitHub CLI unavailable or timed out") from error
    if result.returncode:
        raise PRActionError(f"GitHub PR operation failed (exit {result.returncode})")
    return result.stdout.strip()


def checked_pr(branch: str) -> dict[str, Any]:
    """Read a PR from the intended repository, rejecting unmatched/stale metadata."""
    try:
        selected = valid_branch(branch)
        checked_checkout()
    except UpdateError as error:
        raise PRActionError(str(error)) from error
    if selected == "main":
        raise PRActionError("Select a feature branch with an open pull request")
    fields = ",".join(
        (
            "number",
            "title",
            "url",
            "state",
            "isDraft",
            "headRefName",
            "headRefOid",
            "headRepositoryOwner",
            "baseRefName",
            "statusCheckRollup",
        )
    )
    raw = gh("view", selected, "--json", fields)
    try:
        info = json.loads(raw)
    except ValueError as error:
        raise PRActionError("GitHub returned invalid PR metadata") from error
    if not isinstance(info, dict):
        raise PRActionError("GitHub returned invalid PR metadata")
    number, sha = info.get("number"), info.get("headRefOid")
    head_owner = info.get("headRepositoryOwner")
    if (
        type(number) is not int
        or number < 1
        or not isinstance(sha, str)
        or HEAD.fullmatch(sha) is None
        or info.get("headRefName") != selected
        or not isinstance(head_owner, dict)
        or head_owner.get("login") != "CodeBBQ"
        or info.get("state") != "OPEN"
        or type(info.get("isDraft")) is not bool
        or not isinstance(info.get("baseRefName"), str)
        or not isinstance(info.get("title"), str)
        or not isinstance(info.get("url"), str)
        or info["url"] != f"https://github.com/{REPOSITORY}/pull/{number}"
    ):
        raise PRActionError("No matching open pull request for this branch")
    rollup = info.get("statusCheckRollup")
    if not isinstance(rollup, list):
        raise PRActionError("GitHub returned invalid check metadata")
    return {
        "number": number,
        "title": info["title"][:240],
        "url": info["url"],
        "branch": selected,
        "base": info["baseRefName"][:160],
        "head": sha,
        "draft": info["isDraft"],
        "checks_green": bool(rollup)
        and all(
            isinstance(check, dict)
            and check.get("conclusion") in SAFE_CHECK_CONCLUSIONS
            and check.get("status") in {"COMPLETED", None}
            for check in rollup
        ),
    }


class PRActions:
    def __init__(self, updater: SelfUpdater) -> None:
        self.updater = updater

    def preview(self, branch: str) -> dict[str, Any]:
        return checked_pr(branch)

    def perform(
        self,
        branch: str,
        number: int,
        expected_head: str,
        merge: bool,
        confirmation: str,
    ) -> dict[str, Any]:
        if (
            type(number) is not int
            or number < 1
            or not isinstance(expected_head, str)
            or HEAD.fullmatch(expected_head) is None
            or type(merge) is not bool
            or confirmation != f"{'merge' if merge else 'ready'}:{number}:{expected_head[:12]}"
        ):
            raise PRActionError("Explicit PR confirmation or revision is missing")
        if not UPDATE_LOCK.acquire(blocking=False):
            raise PRActionError("Another PR action is already running")
        try:
            try:
                self.updater.owner_pid()
                if self.updater.status().get("status") in ACTIVE:
                    raise PRActionError("Service update in progress")
                if branch not in self.updater.branches()["branches"]:
                    raise PRActionError("Selected branch is not advertised by origin")
            except UpdateError as error:
                raise PRActionError(str(error)) from error
            current = checked_pr(branch)
            if current["number"] != number or current["head"] != expected_head:
                raise PRActionError("Pull request changed; reopen the menu and inspect it again")
            if merge and not current["checks_green"]:
                raise PRActionError(
                    "Merge requires completed successful PR checks on the exact head"
                )
            if current["draft"]:
                gh("ready", str(number))
            if merge:
                # Guard against the PR changing while its draft status is updated.
                refreshed = checked_pr(branch)
                if (
                    refreshed["number"] != number
                    or refreshed["head"] != expected_head
                    or refreshed["draft"]
                    or not refreshed["checks_green"]
                ):
                    raise PRActionError("PR changed after ready; merge not attempted")
                gh("merge", str(number), "--merge", "--delete-branch")
            return {
                "status": "merged" if merge else "ready",
                "number": number,
                "url": current["url"],
                "head": expected_head,
            }
        finally:
            UPDATE_LOCK.release()
