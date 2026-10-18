# AGENTS.md — Agent Control Service

Repository policy for humans and coding agents in `CodeBBQ/agent-control`.

## Authority and startup

Precedence when sources disagree:

1. The owner's explicit instruction in the current task.
2. Current **owning repository** source, configuration, tests, actual branch/HEAD/diff and live scoped GitHub issues/PRs.
3. Accepted decisions and focused current documentation of the owning repository.
4. This repository's `PROJECT_MEMORY.md`.
5. Old conversation, agent summaries, handoffs and speculative plans.

Read `AGENTS.md`, `PROJECT_MEMORY.md`, the current scoped issue and PR, and the smallest relevant code/tests before substantial technical recommendations, agent handoff, implementation, architecture changes or review. Establish current branch, exact HEAD, base and relevant diff before continuing prior work. For cross-repo integrations also inspect the relevant authoritative source repository rather than reconstructing facts from memory. If evidence conflicts, follow the newer owner/source authority and report the conflict.

Do not transform an investigation into implementation or an independent review into a code-edit task. For uncertain or cross-boundary behavior follow `OBSERVE -> DEFINE INVARIANT -> INVESTIGATE -> IMPLEMENT -> VERIFY -> REVIEW`. Scale process down for straightforward low-risk mechanical work.

## Product boundary and truth model

The product is a **local-first development coordination dashboard**, not a runtime process observer, general GitHub replacement, remote process controller or autonomous agent orchestrator.

- The durable unit is a **workstream**. An **agent run** is one temporary execution assigned to it; neither a chat title, issue, PR nor PID is proof of a live agent.
- The canonical roadmaps, accepted decisions, source code, PRs, branch HEADs, reviews and CI belong to each owning GitHub repository. This service projects those facts and holds only additional coordination records.
- Show evidence provenance explicitly: GitHub-observed, agent-reported, owner-accepted, runtime-verified, inferred or unknown. Do not convert one into another.
- Successful CI must match the **exact current target HEAD**. Green earlier CI, closed issue, finished agent run, positive review and owner-accepted release are separate assertions.
- A missed agent report or heartbeat means **stale/unconfirmed**, not necessarily live, crashed or finished. Only a verified adapter may claim current runtime activity.
- Owner decisions/manual tests/merge authorizations are persistent workstream-related actions; an agent ending must not erase them.
- First web product is **read-only**. It may not merge PRs, close issues, start/stop processes, execute commands, change source branches, modify other repositories or resolve owner actions by inference.
- Ordinary issues in the configured allowlist are visible as workstreams without machine registration. Comments, branches, PRs and CI provide conservative observations; they do not establish agent liveness. Local JSON is a separate demo/test mode.
- Do not introduce cloud runtime data infrastructure, user accounts, full orchestration, a large framework, remote management endpoints or extra dependencies without a scoped accepted decision.
- Default web binding is loopback `127.0.0.1`; remote access, write permissions and runtime instrumentation require scoped security/acceptance evidence. Python >= 3.11.
- Distinguish requested behavior, agreed invariant, proposed design, code shipped, and real-host behavior actually verified. Never mark a planned capability as implemented.

Roadmap: #1 (umbrella product contract), #2 (state), #3 (GitHub projection), #4 (UI), #5 (agent reporting), #6 (managed local runtime), #7 (security), #8 (Codex runtime investigation only), #9 (tests/CI/acceptance).

## Architecture and ownership

Keep boundaries narrow and explicit:

- **GitHub project source**: canonical issues/PR/commits/check runs for each configured project. No edits to these projects merely to populate a dashboard.
- **GitHub observation**: normal issue comments, branches and PRs provide status context; background polls are independent of browser traffic. Caches must be marked stale, and no event protocol is required.
- **GitHub adapter**: read-only API querying, pagination, refresh, error/rate handling, provenance and exact-HEAD projection (#3).
- **Domain projection**: workstream, agent-run, owner-action and evidence semantics (#2/#4); not a second canonical roadmap.
- **Web**: FastAPI/Jinja templates, HTML/CSS/JS and read-only routes (#4).
- **Managed runtime**: loopback app, health, local cache, macOS LaunchAgent and optional operator-authorized private Tailscale Serve (#6).
- **Agent reports**: automated machine-event publication is outside this public copy.
- **Optional real Codex runtime adapter**: requires investigation #8 and its own evidence; no speculative agent discovery.

## Security, privacy and external data

- Repository identities and state storage locations are operator-configured allowlists; never implicitly scan all private repositories or a sibling checkout.
- Treat issue titles/bodies, PR content, agent reports and links as untrusted. Validate sizes/types/identifiers; escape or safely sanitize output. No path traversal, symlink escape, arbitrary URL fetch/SSRF or HTML injection.
- Separate read-only dashboard credentials from agent state-write credentials. Do not commit, echo, render or accidentally log tokens, private file contents, raw tracebacks, secrets or user personal data.
- Never treat agent-authored state as approval by the owner, a verified CI result, a signed runtime signal, or authorization to manipulate another project.
- Treat comments as untrusted observations; do not infer owner approval, agent execution or verified results from comment text.
- The web UI is read-only initially: do not add hidden POST operations or general shell commands. Any future browser writes need an explicit authz/CSRF/replay contract.
- Synthetic tests must never mutate real connected repositories, agent-state branches or the owner's filesystem.

## Implementation and acceptance discipline

For uncertain security, concurrency, remote data writes, runtime observation and cross-service behavior:

1. Trace each material requirement to the scoped live issue/decision, observable outcome, acceptance test and real environment needed.
2. Establish an invariant and targeted evidence before prescribing a production fix. Use deterministic fakes for clocks, API pagination/failures, write conflicts and idempotency but do not mislabel them as verified real GitHub or agent runtime behavior.
3. Keep accepted behavioral acceptance tests independent from the implementation. Do not silently skip, weaken, delete or rewrite them for green CI. If an acceptance criterion is wrong, document the contract discrepancy and obtain independent focused review before changing it.
4. Implement bounded coherent slices and their focused regression tests. Do not silently implement later issues or broaden product authority.
5. Classify failures: production defect, test/oracle error, environment problem or unresolved contract. Avoid patching the wrong side of a boundary.
6. Independently review security/concurrency/remote-exposure/runtime-control changes on the exact revision. Review against contract and source, not only test results.

Review convergence: one coherent independent review per sensitive completed slice by default; after blockers are corrected, targeted exact-new-head checks and a **focused closure re-review**, not unlimited general review cycles. Each blocker must identify violated invariant, affected revision/code, reachable mechanism, evidence and verifying test. Separate confirmed blocker from unverified risk or optional improvement. If a root-cause class repeats, investigate and correct the class together; never weaken tests to make CI green.

Conclude with `READY FOR INTEGRATION`, `CHANGES REQUIRED` or `BLOCKED ON CONTRACT/ENVIRONMENT`; green CI alone does not establish user/browser/macOS/Tailscale/live-GitHub acceptance. No review-count limit waives a real blocking defect.

## Git / branches / PR ownership

Branch and PR count follows independent active integration **workstreams**, not agent count, prompts, review cycles or failures.

1. Inspect current issue/PR, current HEAD, ancestry, diff and accepted roadmap before editing. Reuse the existing owning feature branch/PR for subsequent slices, fixes, tests, reviews and documentation.
2. A genuinely independent new feature with no owning branch **may receive an agent-created feature branch without separate permission**, based on current `main` or a documented accepted dependency. Record base, target, reason and dependencies. No branch-per-chat/review/test workaround.
3. Feature-level tests travel with the feature by default. CI infrastructure changes have their own owner. Independent reviewer/test-author roles do not require permanent new branches.
4. One live PR per workstream by default. Stacked PRs need a separately scoped dependency/integration reason. Update existing PRs rather than multiplying them.
5. Use clean worktrees/isolated checkouts for concurrent local agents. Recheck shared HEAD before writing. Do not overwrite unrelated concurrent changes.
6. Integration requires comparing source and destination, preserving accepted corrections/tests/history and explicitly recording obsolete branch/PR references.
7. **Never merge a PR, close an issue, rewrite history, force-push, delete branches or perform destructive operations without explicit owner authorization.**
8. Provide changed HEAD, relevant issue/PR, checks run and outstanding gates after meaningful changes.

An agent prompt should normally reference the live issue and this `AGENTS.md` / `PROJECT_MEMORY.md`, adding only task-specific evidence and prohibitions rather than restating repository policy.
