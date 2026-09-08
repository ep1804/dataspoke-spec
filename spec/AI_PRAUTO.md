# PRauto: Autonomous PR Worker

> This document specifies "prauto" -- an autonomous PR worker that monitors GitHub issues,
> writes code via a headless coding-agent CLI, and submits pull requests. Prauto extends the AI
> scaffold (`spec/AI_SCAFFOLD.md`) with unattended, scheduled development automation.

> **Architecture**: Prauto separates a durable, agent-agnostic *contract* from a deterministic
> *executor* and a thin *scheduler*. The contract defines labels, phase state, the evidence-based
> plan gate, generator ≠ reviewer, deploy ordering, GitHub-as-SSOT, and the security model. The
> executor (`.prauto/heartbeat.sh` with `.prauto/lib/*.sh`) owns each tick's lock, configuration,
> agent probes and selection, dispatch, and finalization. A scheduler supplies cadence and
> supervision; the Hermes binding is one supported scheduler integration (an agent supervisor
> that reports to Slack and detaches a background monitor), while the executor remains the sole
> owner of every tick step.

---

## Table of Contents

1. [Overview](#overview)
2. [Worker Identity and Configuration](#worker-identity-and-configuration)
3. [Executor Cycle](#executor-cycle)
4. [Agent Availability](#agent-availability)
5. [Job State Machine](#job-state-machine)
6. [Issue Discovery Protocol](#issue-discovery-protocol)
7. [Worker Agent Invocation](#worker-agent-invocation)
8. [Dev Cluster and Deploys](#dev-cluster-and-deploys)
9. [PR Lifecycle](#pr-lifecycle)
10. [Write Idempotency](#write-idempotency)
11. [Security Model](#security-model)
12. [Integration with AI Scaffold](#integration-with-ai-scaffold)
13. [Executor and Scheduler](#executor-and-scheduler)

---

## Overview

### What prauto is

Prauto is a scheduled, unattended worker that automates the issue-to-PR pipeline. Each wake:

1. Checks whether a coding agent is available (Claude Code, else Codex; else exit)
2. Claims new work if under `PRAUTO_OPEN_ISSUE_LIMIT`
3. Processes **all** claimed issues (oldest first), each via a self-contained state machine

Two layers, with different lifespans and owners:

- **Contract** — GitHub labels, the phase state machine, the evidence-based plan gate, the
  generator ≠ reviewer rule, deploy ordering, and the security model. This is the part that
  survives any re-implementation; it is specified here.
- **Executor** — performs a tick: concurrency control, configuration, agent availability and
  selection, issue processing, worker/reviewer dispatch, and finalization.
- **Scheduler** — supplies cadence and launches the executor; the reference Hermes binding adds
  an agent supervisor that reports to Slack and monitors the run. It does not select agents,
  inspect quota, or perform issue work.

### Key design decisions

- **GitHub as the SSOT for phase state**: Every wake derives its *next action* from **remote
  GitHub state** (labels, assignees, comments, review status). Phase, retry count, and plan
  approval are re-derived from GitHub on each tick; the executor carries no in-memory phase state
  between ticks. This is what makes the executor tick resumable across a fresh-process scheduler
  launch.
- **Continuity is not GitHub-only**: What a resume *continues* lives outside GitHub phase state.
  Work-product continuity flows through (1) committed checkpoints on the issue branch (pushed,
  linked to the issue, and posted as commit-link comments), (2) agent-native sessions (Claude
  `--session-id`, Codex `thread_id`), and (3) the local `native-sessions/` anchor that gates a
  Codex resume. GitHub is the source of truth for *phase state*; it is not the only carrier of
  *work product*.
- **No uncommitted resume**: Each worker session starts fresh unless the agent-native quota
  resume path is active. The implementation prompt instructs the agent to check the branch for
  existing committed work and continue from there. When the executor regains control, it
  best-effort pushes committed checkpoints, links the branch to the issue, and posts idempotent
  issue comments containing commit links. Uncommitted work from a session that died mid-run is
  discarded with the worktree and is not resumed.
- **Ready-label timestamp as lifecycle anchor**: When `prauto:ready` is set (or re-set), the
  timestamp of that label event marks the start of the current lifecycle. All comment-scanning
  ignores comments before it, enabling clean restarts without manual cleanup.

### Execution environment

Runs on a local developer machine. Requires: a coding-agent CLI with a logged-in account
(Claude Code and/or Codex — both use login-based OAuth, not API tokens), the `gh` CLI
(authenticated), `git`, and a scheduler capable of launching the executor. Docker/K8s/cloud
deployments are out of scope for v1.

---

## Worker Identity and Configuration

### Two configuration tiers

| File | Committed | Purpose |
|------|-----------|---------|
| `config.env` | Yes | Repo-level conventions: labels, branch prefix, max retries, model, org-member filter, reviewer |
| `config.local.env` | No | Instance identity (`PRAUTO_WORKER_ID`), agent choice and turn/budget limits, dev-cluster binding, `ANTHROPIC_API_KEY`, `GH_TOKEN` |

A single machine may run multiple prauto instances (distinct worker IDs) sharing the same
GitHub credential. If `ANTHROPIC_API_KEY` or `GH_TOKEN` is empty, CLIs fall back to system
authentication.

### GitHub identity has two independent axes

`GH_TOKEN` authenticates `gh`-driven GitHub API calls only — issue labels, comments, PR creation.
It does not authenticate `git push`, which goes over SSH and resolves whatever account the local
`git`/`gh` SSH identity belongs to. `PRAUTO_GIT_AUTHOR_NAME` / `PRAUTO_GIT_AUTHOR_EMAIL` set
commit *authorship* independently of both (applied via `git commit --author=...` in the worker's
system prompt). Running prauto under a dedicated bot GitHub account requires all three — API
token, SSH key, and author identity — to resolve to that same account, or commits, pushes, and
API actions end up attributed to different identities. See `.prauto/README.md` §Optional:
Dedicated GitHub Bot Account for the setup.

`PRAUTO_AGENT` selects the worker's coding agent: `claude`, `codex`, or `auto` (the default;
Claude first, Codex fallback — see [Agent Availability](#agent-availability)). Claude turn and
budget configuration applies only to Claude invocations. Codex has neither the Claude
`--max-turns` nor `--max-budget-usd` interface; the executor does not translate or pass those
flags to Codex.

---

## Executor Cycle

Each executor tick runs seven steps in order. Worker and reviewer agents run only when the
executor dispatches them in step 6.

1. **Concurrency gate** — exit if a worker subagent is already running or pending (a prior
   tick's worker that is mid-run or waiting out a token reset). Enforced by the executor's
   durable PID lock; see [Security Model](#security-model).
2. **Load config** — `config.env` + `config.local.env`.
3. **Agent availability** — pick Claude Code, else Codex, else exit and post a
   quota-paused comment on WIP issues. See [Agent Availability](#agent-availability).
4. **Claim a new issue** if under `PRAUTO_OPEN_ISSUE_LIMIT`.
5. **Process all claimed issues** (oldest first, self-contained state machine per issue):
   `prauto:done`/`prauto:failed` skip, `prauto:wip` derives phase and dispatches a worker
   subagent for the actionable phase, `prauto:review` squash-finalizes or addresses feedback.
6. **Dispatch** — one worker subagent per actionable issue (analysis, implementation,
   integration-fix), then a reviewer subagent over the worker's diff where the contract calls
   for adversarial review (implementation).
7. **Finalize** — the executor (not the worker) pushes, opens/updates PRs, posts test
   results, and swaps labels.

**Claim-first, then process-all**: Step 4 counts open issues assigned to this worker (excluding
ready-only restarted issues). If under limit, claims the oldest `prauto:ready` issue. Step 5
loops over all claimed issues.

**Worktree isolation**: Every worker session runs in a dedicated git worktree. The main repo
directory is never the working directory during worker invocations.

**Cadence**: The user-specified interval (hourly in the reference binding). The schedule is owned by the
scheduler binding, not by `config.local.env`.

---

## Agent Availability

The executor probes agent availability with a two-step check per candidate, in order:

1. **Claude Code**: `claude auth status` (checks a logged-in OAuth session), then a minimal
   one-turn dry-run (`claude -p "Reply with exactly: OK" --max-turns 1 --allowedTools ""`).
2. **Codex**: presence of a CLI OAuth session (`~/.codex/auth.json`), then a minimal JSONL
   dry-run (`codex exec --json "Reply with exactly: OK"`).

Both agents are authenticated by **login-based subscription accounts, not API tokens**, so the
probe must invoke the CLI itself — there is no token to inspect out-of-band. If `PRAUTO_AGENT`
is `claude` or `codex`, only that agent is probed; `auto` probes Claude then Codex.

Outcomes:

- An agent passes → it is selected for this wake.
- Neither passes → the executor exits. If a WIP issue exists, post a "Paused" comment (with
  marker); the retry counter is not incremented. On the next wake with an agent available,
  post "Resumed" before continuing.

A dry-run timeout (network slowness) is **not** treated as exhausted — proceed anyway.

### Quota-pause and resume

A worker that dies mid-run on a rate/session-limit exit pauses rather than fails: the executor
posts a pause marker carrying the session id, and the next wake resumes the SAME session on the
SAME agent once quota resets. The marker:

```
prauto(<worker>): Paused — <agent> quota exhausted. Will resume automatically on the next quota window.

To abandon this session and restart from scratch instead of resuming, comment "abandon previous session".

prauto:quota-paused
prauto:agent=<agent>
prauto:session=<session-id>
```

The `prauto:quota-paused` / `prauto:agent=` / `prauto:session=` lines are the machine-readable
state the next wake parses; they are plain text, not HTML comments — they are not secret
(comments require write access, and the session id is already in the local state dir), so hiding
them buys nothing while costing parse robustness.

On the next wake, per WIP issue, derived fresh from GitHub:

1. **Paused?** — the latest prauto pause/resume/restart marker is a pause → handle it.
2. **Abandon override?** — a non-prauto comment reading `abandon previous session` posted after
   the pause marker forces a **restart**, not a resume: a fresh session, with the agent re-selected
   under `PRAUTO_AGENT` (so `auto` falls through to Codex if the paused agent's quota has not
   reset). Posts "Restarting".
3. **Quota reset?** — the paused agent's probe passes → post "Resumed" and **resume** the same
   session, using that agent's native resume command and the captured session id.
4. **Still down** — do nothing, exit; the pause never increments the retry counter.

Session identity is agent-native:

| Agent | Fresh-session identity | Resume identity |
|-------|------------------------|-----------------|
| Claude Code | The executor generates a UUID and supplies it with Claude's `--session-id` option. | That UUID is supplied to Claude's `--resume` option. |
| Codex | Codex creates the thread id. The executor captures `thread_id` from the `thread.started` JSONL event emitted by the fresh invocation. | That captured thread id is supplied to `codex exec resume`. |

The executor persists the captured id before it can post a resumable quota marker. A Codex run
that exits before emitting `thread.started` has no resumable identity and must be restarted rather
than represented as a same-session resume. Resuming is same-agent only: a session cannot migrate
agents, and agent-switch is reachable only through the abandon+restart path. The `plan-approval`
phase is exempt from resume — it waits on a human, not on agent quota.

---

## Job State Machine

### Phases

Minor issues flow `analysis → implementation → integration-fix → pr → complete`. Non-minor
issues insert a `plan-approval` gate between `analysis` and `implementation`; from
`plan-approval`, an approval advances, a counter-proposal loops back to re-analysis, and no
response waits until the next wake.

Phase is always derived fresh from GitHub -- never read from local state.

| Phase | Description |
|-------|-------------|
| `analysis` | Worker reads issue + codebase, produces a plan |
| `plan-approval` | Wait for human approval (retries not counted) |
| `implementation` | Worker writes code, runs unit tests, commits |
| `integration-fix` | Run integration tests; on failure, worker fixes (up to N attempts) |
| `pr-review` | Worker addresses reviewer feedback on existing PR |
| `pr` | Push branch, create/update PR |

### The plan gate is evidence-based

Only `minor` issues skip `plan-approval`, and **the issue body does not decide that**. The
`### Change Size` field an author fills in is a hint. `AGENTS.md §Implementation Workflow` lets a
change skip planning only when **all** of its skip-plan criteria hold:

- touches < 3 files **and** adds/modifies < 60 lines of logic,
- introduces no new API endpoint, DB table/column, pgvector collection, or Airflow DAG,
- requires no cross-layer coordination, and
- the human explicitly signals "just do it".

Prauto satisfies the last criterion structurally: an author's `### Change Size: minor` plus a human
applying `prauto:ready` is prauto's form of that signal. It is necessary but not sufficient — the
first three are evaluated by the analysis phase against **its own plan**, the first artifact that
actually knows the shape of the work. Any single hit downgrades the issue to `medium` and routes it
through `plan-approval`, regardless of what the issue body claimed. An author's `minor` can be
overridden upward; it can never buy a skip the plan's own evidence does not support.

Reading `### Change Size` straight from the issue body would be the self-classification
`AGENTS.md` forbids — *never self-classify a task as "trivial" to skip planning* — and by an author
who has not yet seen the plan. Deferring the judgment to the analysis phase is better evidence, though
it remains an agent session classifying its own plan, not an escape from self-classification.

### Phase derivation from GitHub

On every wake (comment checks scoped to current lifecycle):

1. PR exists for this branch -> `pr`
2. `prauto:plan-review` label present -> `plan-approval`
3. Plan comment exists + "go ahead" reply -> `implementation`
4. Plan comment exists + no approval -> `plan-approval`
5. No plan comment -> `analysis`

### Retry tracking

Each wake posts a marker comment on the issue. `count_heartbeat_comments()` counts markers
within the current lifecycle only (after ready-label timestamp + most recent `Claimed` comment).
At `PRAUTO_MAX_RETRIES_PER_JOB`, the issue is abandoned. The `plan-approval` phase is exempt.

### Job completion and abandonment

| Scenario | Actions |
|----------|---------|
| New issue -> PR | Push, create PR (with `prauto:review` label), remove `prauto:wip`, add `prauto:review` |
| PR feedback | Address with commits, push, post marker |
| Workflow ESCALATE | Do **not** finalize a PR; remove `prauto:wip`/`prauto:plan-review`, add `prauto:failed`, post abandonment comment naming the escalating stage and its findings |
| Max retries | Remove `prauto:wip`/`prauto:plan-review`, add `prauto:failed`, post abandonment comment |

---

## Issue Discovery Protocol

### Label lifecycle

A human sets `prauto:ready`. On claim, prauto removes `prauto:ready`, adds `prauto:wip`, sets
the assignee. Non-minor-ness is a property of the plan, not knowable at claim time, so
`prauto:plan-review` is added when the plan is posted ([the plan gate is evidence-based](#the-plan-gate-is-evidence-based))
and removed on approval. On success the issue and PR both move `prauto:wip` → `prauto:review`; once approved and
squash-finalized, both move to `prauto:done`. On failure, `prauto:wip` is replaced with
`prauto:failed`. An unclaimed issue simply stays `prauto:ready`.

### Search and claiming

Issues discovered via `gh issue list` filtered by `prauto:ready`, sorted oldest-first.
Org-member filter on by default; disable in `config.local.env` (`PRAUTO_GITHUB_ISSUE_FROM_ORG_MEMBERS_ONLY`).

**Optimistic claim protocol**: Check for `prauto:wip` -> record timestamp, add label ->
re-fetch, check for competing claims within window -> remove `prauto:ready`, set assignee, post
claim comment.

### Issue restart protocol

To restart an issue: remove all `prauto:` labels except `prauto:ready`, unassign worker, delete
working branch/PR. The ready-label timestamp ensures all comment-scanning functions
automatically ignore stale comments from previous attempts.

---

## Worker Agent Invocation

### Multi-phase execution model

The worker is a headless coding-agent session (Claude Code or Codex), one per phase. The executor
supplies the phase's goal and bounds; the worker runs the phase's prompt template
(`.prauto/prompts/`) with the agent's native tool scoping.

### Agent execution adapters

The executor keeps a separate CLI adapter for each agent. A shared phase contract does not imply
shared command-line flags or output formats.

- **Claude Code** receives its native session id, prompt/system-prompt, tool-scoping, turn-cap,
  and optional budget-cap arguments. Its structured result is parsed using Claude's JSON output
  contract.
- **Codex fresh sessions** run with `codex exec --json` and the workspace-write sandbox. Codex
  stdout is JSONL: the executor reads the `thread.started` event to persist its `thread_id`, then
  reads terminal events for the worker result and quota classification. It must not fabricate a
  Codex id or pass an executor-generated id as though Codex had accepted it.
- **Codex resumed sessions** run with `codex exec resume --json <thread-id> <prompt>`. Resume
  accepts the prior Codex identity; it does not accept Claude session, tool, turn, budget, or
  fresh-session sandbox flags. The executor uses the JSONL stream on a resumed run for result and
  quota classification as well.

JSONL is a protocol boundary, not display text: parsers select records by their event type and
fields, preserve the raw stream as diagnostic evidence, and do not infer a thread id or quota
state from human-readable prose when the required structured event is absent.

| Phase | Tools | Turn cap |
|-------|-------|-----------|
| Analysis | Read + Write (plan file only) + limited git | `PRAUTO_MAX_TURNS_ANALYSIS` |
| Implementation | Read + Write + Edit + subagents + workflow + limited Bash (git; `uv sync`/`uv run` pytest, python3, ruff, mypy; `npm run`, `npx prettier`, `npx tsc`, `npx eslint`, `pnpm`) | `PRAUTO_MAX_TURNS_IMPLEMENTATION` |
| Integration fix | Same as implementation | `PRAUTO_MAX_TURNS_INTEGRATION_FIX` |
| PR review | Same as implementation | `PRAUTO_MAX_TURNS_IMPLEMENTATION` |
| Squash commit / Feedback response | No tools (text only) | 1 |

**Denylist (all phases)**: `git push`, `rm -rf`, `sudo`, `kubectl`, `helm`, `curl`, `wget`,
`gh`, `Read(.prauto/config.local.env)`, `Read(.prauto/state/*)`, `WebFetch`, `WebSearch`. It binds
the parent session only — see [Security Model](#security-model) for what it does and does not
enforce.

**Branch-based continuity**: On restart, the prompt instructs the agent to check for existing
commits on the branch and continue from there.

### The implementation phase runs the AGENTS.md workflow

Prauto's implementation phase is the unattended form of `AGENTS.md §Implementation Workflow`
steps 4–9: it drives the generator → adversarial-reviewer → one-fix-pass loop over the plan's
`stages` (with `security` flagging stages that need `security-reviewer` in parallel). In the
Claude binding this is `.claude/workflows/wf-minimal.js`; in the Codex binding it is the
equivalent orchestration expressed in the Codex worker prompt. The analysis phase emits
`stages` (in plan order, with inner arrays retained as grouping metadata; generator stages execute
serially because they commit in one shared worktree) and `security` alongside its plan.

Review is therefore **per-stage and adversarial** — each generator is evaluated by a separate
context before later stages build on its output, upholding the generator ≠ reviewer rule that
exists to prevent the self-praise failure mode.

Each generator commits its own stage to the branch as its final action — the workflow's
commit-per-stage contract — and a REVISE fix pass produces a follow-up commit. Generator stages
execute serially because they share one worktree and Git index; reviewer passes within a stage may
still run concurrently. Reviewers stay read-only and evaluate the committed changes. Commits land
on the private `prauto/I-*` worktree branch only, never `master`, and are attributed to the worker
via `--author`. Before integration or PR finalization, the parent requires the exact
`PRAUTO_WORKFLOW_OUTCOME: COMPLETE` sentinel and a clean worktree. Progress is therefore durable
per stage: a run that dies mid-workflow loses only the stage in flight, and a quota-pause resume
re-enters a branch whose committed state matches the session's memory. The executor publishes
checkpoint commits as soon as it regains control; these commits are unreviewed intermediate
progress, and each published commit is recorded as an idempotent issue comment.

An ESCALATE outcome halts the workflow at the escalating stage group, so later stages never run and
the branch holds a partial implementation. Prauto must not carry that forward to tests or a PR: it
abandons the job ([Job completion and abandonment](#job-completion-and-abandonment)) rather than
finalizing.

### Executor-owned review gate

In addition to the in-workflow per-stage review, the executor runs a **final adversarial review
gate** over the worker's committed diff before the PR is opened: a fresh reviewer subagent — a
separate context that has not seen the worker's session — reads the diff against
`scaffold/roles/reviewer.md` and returns a verdict. The reviewer is a genuinely separate process,
not the worker's session. A REVISE verdict feeds one fix pass; an ESCALATE
abandons the job as above. The gate is mandatory for `implementation`; analysis, integration-fix,
and pr-review do not re-open it.

Deploys stay executor-owned. Prauto's analysis phase never emits `k8s-helm` as a stage: that
stage would deploy under whatever its kubeconfig points at, ignoring the worker-cluster binding and
the api-then-frontend ordering ([Branch image deploys](#branch-image-deploys)), and it carries no
reviewer. All cluster mutation runs from the executor against `$PRAUTO_DEV_ENV_FILE`.

---

## Dev Cluster and Deploys

### Per-worker dedicated cluster

Each prauto instance binds to **its own dev-profile cluster**, selected by `PRAUTO_DEV_ENV_FILE`
(default `helm-charts/.env.dev`). This binding is what makes provisioning and deploying safe to
automate at all: prauto never contends with a human engineer's cluster for the dev-env lock, and
the blast radius of anything it does — a bad chart, a wedged namespace, a destructive reset —
stops at a cluster only prauto uses.

The env file resolves under `$REPO_DIR`, **never the worktree**. This is a security property, not
a path convention: the worktree holds branch-authored content, so resolving cluster credentials
from it would let a branch redirect prauto's deploys and resets at a cluster of its choosing.

### Provisioning

The cluster is prauto's to create, not a precondition it waits on. When
`./helm-charts/bin/health-check.sh --env-file $PRAUTO_DEV_ENV_FILE --keep-lock` exits 1 — probes ran and the
deployment is unhealthy or absent — prauto runs a full
`install.sh --profile dev --env-file $PRAUTO_DEV_ENV_FILE` and re-checks; the cluster stages are
skipped only if **provisioning itself** fails. Exit 2 is a local setup fault, not cluster evidence
(`HELM_CHART.md` §Health Check): prauto reports it and provisions nothing, so a missing `kubectl`
or an unresolvable context cannot trigger an unsupervised cluster build. Gated by
`PRAUTO_CLUSTER_PROVISION_ENABLED` (default `true`). The health check is run under a wall-clock
backstop and in a private `TMPDIR`, because this worker is unsupervised: nothing outside it would
notice a check that never returns, and a run the backstop stops must not leave behind the
kubeconfig copy the check writes. A fired backstop counts as exit 1. Every
`install.sh`/`health-check.sh` invocation
carries `--env-file $PRAUTO_DEV_ENV_FILE`; without it both default to `helm-charts/.env.dev`, the
shared cluster the per-worker binding exists to avoid. The health check additionally carries
`--keep-lock` and runs with stdin closed: an unattended gate must never wait on a release prompt,
and prauto meets a held lock through its own acquire, which skips on 409.

Provisioning cost does not count against `PRAUTO_MAX_RETRIES_PER_JOB` — standing up a cluster is
not an attempt at the issue, and charging it would abandon jobs for infrastructure latency that
says nothing about the work.

**Autopilot abort mode**: on GKE Autopilot, a GMS scale-up timeout aborts `install.sh` before the
DataHub ingress+PAT step. The resume is `--from-component datahub`, not `dataspoke-infra`. Note
that fragmented `--from-component` resumes skip the env-sync step and leave stale
`DATASPOKE_DEV_*` credentials in the env file; those are rebuilt from cluster secrets.

### Branch image deploys

Cluster stages test **deployed artifacts**, so the branch's code reaches them only by being
built and deployed. These deploys run the **worktree's** `install.sh` / `build-image.sh`, so
`docker build` runs over the branch's `src/`, its `Dockerfile`, and its chart — all three are under
test. This is deliberate: a branch that changes a Dockerfile or a chart can only be proven by
executing that change, which a trusted base checkout cannot do.

| Diff touches | Deploy (run from the worktree) |
|---|---|
| `src/{api,backend,shared}` | `install.sh --profile dev --env-file $PRAUTO_DEV_ENV_FILE --components api` |
| `src/frontend/` | `install.sh --profile dev --env-file $PRAUTO_DEV_ENV_FILE --components frontend` (plus a forced rollout restart — a belt-and-braces guard so the stage never tests a stale pod, independent of the chart's digest stamping) |

**Trusted cluster selection, untrusted build — the one invariant of the deploy.** The two halves
resolve from different trees on purpose: the `--env-file` always resolves from `$REPO_DIR`, never the
worktree, so a branch cannot redirect which cluster is hit; the build/deploy scripts run from the
worktree, so branch infra changes are actually exercised. Provisioning and health-check stay on
`$REPO_DIR` — trusted infra bring-up, not the thing under test.

**Order is api, then frontend — this is a correctness constraint.** A `--components api` upgrade
is a full-release upgrade that reverts `frontend.enabled → false`, deleting the cluster frontend.
Running frontend first therefore destroys the very UI the E2E stage needs, and the failure
surfaces as a confusing E2E error rather than as a deploy fault.

The E2E gate is `src/frontend/`, `tests/e2e/`, or `src/api/`. `src/api/` counts because it is the
contract surface the UI consumes; `src/backend/` and `src/shared/` do not, because api-wired already
proves those over REST against the freshly deployed API image, and a frontend rebuild plus a browser
run would only re-prove it at far higher cost.

---

## PR Lifecycle

### Branch naming

`prauto/I-{issue_number}` (e.g., `prauto/I-42`). Created as isolated git worktrees.

### Push and PR creation

After implementation, the executor (not the worker) pushes the branch, links it to the issue's
Development section, posts commit-link comments for any unpublished commits, checks for an
existing PR, and creates one if none exists (with `prauto:review` label, assignee, optional
reviewer). The same checkpoint publication runs after worker-led review and integration-fix
stages. A strict push remains the finalization gate; checkpoint pushes are best-effort so a
temporary GitHub outage does not erase local committed progress.

### PR review handling

Issues with `prauto:review` label are checked for unaddressed non-prauto comments. The
feedback-addressed marker breaks the re-pickup loop; new reviewer comments after the marker
make the PR actionable again.

### Test execution

Prauto runs the unattended form of the protocol in [`TESTING.md`](TESTING.md), which is
authoritative for layers, commands, and constraints. Stages run in order; each is skipped when
the diff does not reach its layer. Each stage names its **actor**: the worker runs a stage from
its prompt template inside the session's tool whitelist, while the executor runs a stage
directly and invokes the worker only for fix sessions.

**Pre-flight gate** *(executor)*: the health check in its
[Provisioning](#provisioning) form — `--env-file $PRAUTO_DEV_ENV_FILE --keep-lock` — runs before
any integration work. On exit 1, prauto provisions its own cluster and re-checks
([Provisioning](#provisioning)); the integration and E2E stages are **skipped, not failed** only
if provisioning fails. On exit 2 it reports the setup fault, provisions nothing, and skips those
stages rather than failing the issue. An unprovisionable cluster is evidence about the
infrastructure, not the branch, so failing it would burn retries against unrelated code.

**Environment**: the executor's integration and E2E stages source the worker's env file
(`$PRAUTO_DEV_ENV_FILE`, resolved under `$REPO_DIR`) via `set -a` (the file carries no `export`
prefixes) and hold the dev-env lock at `$DATASPOKE_DEV_LOCK_URL`.

**Stage 1 -- Static gates** *(worker)*: `uv run ruff check src/ tests/` and `uv run mypy src/`,
invoked as **checks, never `--fix`** — prauto verifies the author-run gate rather than mutating
the diff until it passes. Frontend-touching work adds `npx tsc --noEmit` and `npx eslint src/`
from `src/frontend/`; diffs touching `tests/e2e/` add `pnpm -C tests/e2e typecheck`. These are
the four author-run gates of [`TESTING.md §CI Behavior`](TESTING.md#ci-behavior); no
`.github/workflows/` exists, so they are the only thing standing between prauto and a red `dev`.

**Stage 2 -- Unit** *(worker)*: `uv run pytest tests/unit/`; frontend-touching work also runs
`pnpm -C src/frontend test` (offline, mocked). Needs no cluster and no lock.

**Stage 3 -- Integration fix loop (pre-push)** *(executor; worker for fixes)*: after
implementation, under the dev-env lock. A diff touching `src/{api,backend,shared}` deploys the
branch's API first ([Branch image deploys](#branch-image-deploys)) so the tests reach the
branch's code rather than a stale image. Then spot (`tests/integration/spot/`) and api-wired
(`tests/integration/api_wired/`) run as **two separate groups**, never mixed — a mixed run puts
competing Airflow load on the cluster and flakes on timing. The split binds every integration
invocation, Stage 5 included. Failures feed the worker's fix loop up to
`PRAUTO_INTEGRATION_FIX_MAX_RETRIES`.

**Stage 4 -- E2E (Playwright)** *(executor; worker for fixes)*: runs when the diff touches
`src/frontend/`, `tests/e2e/`, or `src/api/` ([Branch image deploys](#branch-image-deploys)), and
acquires the dev-env lock for its own run, strictly after the integration groups release theirs. It
deploys the branch's frontend, then runs `pnpm -C tests/e2e test`.

- This stage gives prauto a **cluster + browser dependency** no other stage has.
- The hold is separate rather than inherited because the integration loop has another caller —
  the PR-review path — where E2E is not wanted. The cost is bounded: if another owner takes the
  lock in the gap, E2E skips cleanly, and E2E's own setup reset-seeds rather than depending on
  state inherited from the integration groups.
- Ordering is a constraint, not a preference. Two reasons compound: the frontend deploy rolls the
  API pod, and `--components api` would delete the cluster frontend if it ran second. E2E must
  land strictly after the integration groups and never run concurrently with them.
- `PRAUTO_E2E_FIX_MAX_RETRIES` defaults to `1`, which is **report-only**: the executor invokes a fix
  session only on a non-final attempt, so a single attempt runs the suite and reports the result
  without fixing. Raising it buys fix attempts at a full rebuild + redeploy each.

**Stage 5 -- Final test report (post-push)** *(executor)*: runs unit + integration tests —
integration under Stage 3's two-group split — and posts results as collapsible PR comments.

### What a green run proves

The cluster-dependent stages reach the API and UI over ingress, so they test **deployed
artifacts** — never the worktree directly. Those artifacts are built from the branch: the Stage 3
and Stage 4 deploys build their images from the worktree source, so api-wired runs against the
branch's own API rather than a stale one — the staleness that would otherwise leave branch API code
unproven is genuinely closed, not relocated. When a deploy is skipped because the diff does not
touch that layer, the stage runs against the image already on the cluster, which is correct: an
untouched layer has nothing new to prove.

The remaining gap is the deploy's own fidelity. A green run proves the branch's built images pass
against a dev-profile cluster running stub clients by default
([`TESTING.md §Stub Toggles`](TESTING.md#stub-toggles-runtimeconfig)) — not that the code holds
against real LLM, Redis, or notification backends.

### Squash-finalize

**Trigger**: PR has `prauto:review` label, assigned to worker, mergeable, clean, latest review
APPROVED.

**Steps**: Rebase on base -> generate squash commit message (1-turn worker, no tools) ->
`git reset --soft` + commit -> force-push with lease -> post the squashed commit link on the
issue -> update PR title -> labels to `prauto:done` on issue + PR. Does **not** merge or close --
left to the human.

**Commit format**: Conventional commit with max 5-line body, issue/PR reference,
`Co-Authored-By` trailers.

---

## Write Idempotency

### Comment idempotency

| Context | Keyword | Idempotent? |
|---------|---------|-------------|
| Claim | `Claimed` | No -- always fresh (anchors retry counting) |
| Abandonment | `Abandoning` | Yes |
| Plan | `Plan` / `Plan (rev N)` | Yes |
| Quota pause | `Paused` | Yes |
| Heartbeat | `Heartbeat` | No -- each is a new retry marker |
| Implementation start | `Heartbeat -- implementation starting` | No |
| Workflow escalation | `Heartbeat -- workflow escalated` | No |
| Integration fix | `Heartbeat -- integration test fix loop` | No |
| Review/Feedback response | `Review response` / `Feedback response` | No -- multiple valid |

### Optimistic claim locking

Check-then-add with timestamp-based verification window. Not fully atomic but catches most
races; a no-op safeguard for single-worker deployments.

---

## Security Model

### What the phase whitelist is

The per-phase tool whitelist is **defense-in-depth, not an enforced boundary**. It raises the
cost of casual misuse and catches accidental and low-effort failure modes, which is real value.
It does not contain a determined or prompt-injected session, and must not be relied on as though
it does.

The implementation phase grants general-purpose code execution (e.g. `Bash(uv run python3 *)`),
which is general-purpose code execution: a session that shells out from Python reaches every
command the denylist names — `kubectl`, `helm`, `curl`, `wget`, `git push`. Sessions also run with
permissions auto-approved, and no path restriction bounds `Write`/`Edit`. The grant is
load-bearing (prauto writes Python), so the gap is inherent to the phase's purpose rather than
an oversight to be patched by trimming the list.

**Delegation removes even the speed bump.** Tool scoping does not propagate to subagents; a
subagent's own definition governs its tools. The project's generators (`backend`, `test`,
`k8s-helm`) each declare unrestricted `Bash` with no denylist, so granting subagent delegation
means delegated work reaches `kubectl`, `helm`, `git push`, and `curl` directly — no reach-around
needed. The parent denylist binds the parent session and nothing beyond it. The same holds for
`Read(.prauto/config.local.env)`: a subagent reads that file directly, exposing
`ANTHROPIC_API_KEY` and `GH_TOKEN`. That denial was never the real boundary regardless — both are
already exported into every child's environment — so delegation widens an existing exposure
rather than opening a new one.

Turn and budget caps thin out the same way. A parent turn cap bounds the parent coding-agent
session only; subagents take their own limits, and no project agent sets one. Thus
`PRAUTO_MAX_TURNS_*` stops bounding delegated work. Whether a budget cap aggregates across
subagents is **unverified** —
treat delegated spend as unbounded until someone establishes otherwise.

### The executor boundary and its limits

The executor's reviewer is a **separate process** with its own context, so the "generator reviews
its own work" escape does not exist: the reviewer was never in the worker's session and inherits
no tool grants from it. This strengthens the generator ≠ reviewer rule.

The worker and reviewer agents have terminal and file access that is broader than the executor's
phase-specific tool whitelist. The cluster binding (`$PRAUTO_DEV_ENV_FILE` resolved from
`$REPO_DIR`) is therefore the primary containment boundary, not the tool whitelist.

| Layer | Restriction | Enforced? |
|-------|-------------|-----------|
| Coding-agent tools | Phase-specific whitelists | No — speed bump; `uv run python3` reaches around it, subagent delegation bypasses it |
| Network access | No web fetch, curl, wget | No — `npx`/`pnpm dlx` fetch and execute arbitrary packages |
| Cluster access | No kubectl, helm for the parent session | No — same reach-around; generators grant `Bash` outright; executor deploys via `install.sh` |
| Destructive ops | No rm -rf, sudo | No — speed bump only |
| Git push | Only executor pushes | No — speed bump; still valuable (see below) |
| Issue author | Org-member filter (on by default; disable in `config.local.env`) | Yes — `PRAUTO_GITHUB_ISSUE_FROM_ORG_MEMBERS_ONLY` |
| Turn limits | Per-job caps | Parent only — subagents take their own limits; none set |
| Budget limits | Per-job cap | Unverified across subagents |
| Concurrency | Max open issues + a single live worker per wake | Yes — `PRAUTO_OPEN_ISSUE_LIMIT` (default 1) + the executor's PID lock |
| Cluster blast radius | Worker-dedicated dev cluster (default points at the shared one) | Partly — `--env-file` from `$REPO_DIR` pins where deploys/resets land, even for the worktree-run deploy scripts; a delegated `Bash` session still reaches any context in the machine's kubeconfig |
| Secrets | Gitignored + denylist | No against delegation — a subagent `Read`s `config.local.env` directly, and `ANTHROPIC_API_KEY`/`GH_TOKEN` are already in the child's environment |

**Why the push separation still earns its place**: keeping `git push` out of the worker's tool
vocabulary means a confused or drifting session does not push to an unexpected branch or remote
in the ordinary course of its work. It is a speed bump against accident, not a control against
intent.

### Prauto executes unreviewed branch code

This is inherent, not incidental: test code must come from the branch to test the branch, so any
stage that runs integration or E2E executes code the branch authored, before a human has read it.
No arrangement of the deploy removes this; it is the cost of testing a branch at all.

- Branch-authored `conftest.py` and `tests/e2e` package scripts run **under the executor** on the
  dev machine, not inside a builder.
- The branch's own `install.sh` / `build-image.sh` run **under the executor** during the deploy
  stages, and their `docker build` runs over branch source, so branch build-time content
  (`package.json`, `next.config`, the Dockerfile, the chart) executes on the dev machine and
  in-cluster. This is the accepted cost of testing a branch's infra changes: proving them requires
  running them ([Branch image deploys](#branch-image-deploys)).
- All of this precedes `finalize_issue_pr` — it happens before the PR **exists**, so there is no
  point at which a human could have reviewed it first.

The one thing the branch cannot rewrite is which cluster it lands on: the `--env-file` resolves from
`$REPO_DIR`, so branch build code runs — but always against the worker's configured cluster, never
one of the branch's choosing.

---

## Integration with AI Scaffold

| Scaffold element | Integration |
|---|---|
| `CLAUDE.md` / `AGENTS.md` | Gives the worker full project context automatically; `AGENTS.md`'s Implementation Workflow is what the implementation phase runs |
| `.claude/settings.json` | Permission prompts do not apply — sessions run with permissions auto-approved; the denylist is prauto's own layer |
| `.claude/agents/` / `.codex/agents/` | The implementation phase delegates to the generator and reviewer subagents; their definitions govern their tools and turns, their bodies point at the canonical role definitions in `scaffold/roles/` |
| `.claude/workflows/` | `wf-minimal.js` drives the Claude binding's per-stage generate → review cycles (Codex expresses the equivalent orchestration in its worker prompt) |
| `scaffold/roles/` | Canonical generator/evaluator roles — the executor's final review gate reads `reviewer.md` directly |
| `scaffold/contracts/` | `reviewer-verdict.schema.json` is the verdict schema the review gate validates against |
| `spec/` hierarchy | Analysis phase reads specs per `AGENTS.md` |

Prauto is self-contained — it does not modify `.claude/` files. The scaffold serves
interactive sessions; prauto serves unattended automation. The dependency runs one way and is
load-bearing: prauto's containment and turn bounds for delegated work are whatever the agent
definitions say they are.

---

## Executor and Scheduler

The contract above is executor-agnostic. The repository's executor is the Bash harness
(`.prauto/heartbeat.sh` + `.prauto/lib/*.sh`); a scheduler is any trigger that invokes it on a
cadence. They split work by durability: the executor owns lock, configuration, issue claiming and
phase derivation, agent probing and selection, dispatch, and finalization; the scheduler owns
cadence and launch only.

### The executor: the bash harness

`.prauto/heartbeat.sh` implements the seven-step [Executor Cycle](#executor-cycle)
deterministically. The reasoning surfaces stay on the coding agents (plan gate, adversarial
review, escalation); the executor owns the deterministic envelope:

| Concern | Mechanism |
|---|---|
| Concurrency gate | `lib/state.sh` PID lockfile with a `kill -0` stale-check — a second wake never runs a worker against a worktree another is mid-flight on |
| Cleanup | a `trap` removes the live worktree and releases the lock on any exit, so a dead worker leaves neither a dirty tree nor a stale lock |
| Ephemeral reset | each wake sweeps orphaned worktrees first — *uncommitted* work is invisible to resume; committed checkpoints and the Codex `native-sessions/` anchor survive the reset and gate a resume |
| GitHub identity | the executor resolves `gh api user` once at startup and asserts it against `PRAUTO_GITHUB_EXPECTED_ACTOR` (when set), so every comment/label/assignee is attributed to the worker account, never the keyring fallback |
| SSOT readers | `lib/issues.sh` derives phase, retry count, and plan approval as exact `gh`+`jq` readers — never prose-derived |
| Agent dispatch | `lib/agent.sh` invokes the coding agent per phase, honoring `PRAUTO_AGENT` and the [Quota-pause and resume](#quota-pause-and-resume) session-resume contract |

The executor is self-contained: run `bash .prauto/heartbeat.sh` directly for a manual tick.

### The scheduler

The scheduler invokes the executor on a cadence. It probes no agent and does not pre-set
`PRAUTO_AGENT`: agent selection is the executor's own job (`lib/agent.sh`
`select_agent`). A scheduler's responsibilities are cadence, launch, and — in the reference
Hermes binding — supervision and reporting (an agent that reports to Slack and detaches a
background monitor). It performs no issue work and holds no phase state; that remains the
executor's, re-derived from GitHub each tick.

### Reference binding: an agent-supervised Hermes cron job reporting to Slack

The reference scheduler binding is an **agent-supervised Hermes cron job**. Each tick wakes a
supervisor agent that (1) reports the trigger to Slack, (2) detaches the executor against the
durable local checkout when it is idle, and (3) spawns the background monitor
(`.prauto/scheduler/monitor.sh`) that keeps posting brief Slack notes until the coding agent
finishes. The supervisor itself performs no issue work and holds no state between ticks; the
detached executor and monitor are the long-running parts, and the executor's PID lock makes an
"already running" tick a no-op. This pattern is appropriate for a persistent, single-host
scheduler that permits child processes to outlive the trigger. Hermes is one such scheduler, not
a requirement.

**Prerequisites**

- Hermes Agent installed with its gateway running; the cron scheduler fires only while the
  gateway is available. Follow Hermes's operator documentation for gateway installation and
  status checks. Slack reporting reuses the gateway's Slack credentials via `hermes send` (no
  running gateway required for the bot-token path).

- The repo checked out locally (the job's `workdir`), with `config.local.env`, the `prauto:*`
  labels, and a Slack channel configured in place.

**Job definition (canonical)** — every field is preserved in the env files so the job is
reproducible from the repo. Repo-level fields live in `config.env` (committed); instance-identity
fields live in `config.local.env` (gitignored — the repo is public).

| Field | Env var | File | Value |
|---|---|---|---|
| `schedule` | `PRAUTO_SCHEDULER_HERMES_SCHEDULE` | config.env | `15 * * * *` (hourly at :15 past; a tick with no actionable issue is a no-op) |
| `name` | `PRAUTO_SCHEDULER_HERMES_NAME` | config.env | `DataSpoke PRauto heartbeat` |
| `skills` | `PRAUTO_SCHEDULER_HERMES_SKILL` | config.env | `prauto-executor` (the supervisor skill) |
| `prompt` | (canonical prompt below) | config.env | the supervisor procedure — report, detach, monitor |
| `workdir` | `PRAUTO_SCHEDULER_HERMES_WORKDIR` | config.local.env | the local checkout |
| `deliver` | `PRAUTO_SCHEDULER_HERMES_DELIVER` | config.local.env | `local` — the supervisor's final response is archived; Slack reporting is explicit via `hermes send` |

Create the Hermes job from the table's values using Hermes's cron interface. The supervisor
agent does not perform executor work: it launches the executor and monitors it. After the
executor and monitor are detached, executor success, failure, and phase state are observed
through GitHub and the executor log — the monitor relays those to Slack, so Slack is the human
surface, not the SSOT.

The canonical monitor is `.prauto/scheduler/monitor.sh`; the supervisor detaches it through
`.prauto/scheduler/launch.sh`, which owns the mechanical envelope — check the executor lock,
detach the executor, verify it survived its first seconds, then detach the monitor. Both detaches
use `.prauto/scheduler/daemonize.py`, a setsid double-fork that runs the executor and monitor in
their own sessions (re-parented to launchd) so they survive the supervisor turn's process-group
teardown — the Hermes terminal tool tears down with `killpg`, which misses setsid children. The
monitor posts its own Slack notes via `hermes send` (no LLM, no running gateway required). The
executor's PID lock and GitHub idempotency make overlapping ticks safe.

**The cron tick is not where the work happens.** The tick detaches the executor and the monitor;
the executor's agent invocations are the long-running part. GitHub is the SSOT for phase state
([Overview](#overview)) — each wake re-derives its next action from remote GitHub state — while
work-product continuity flows through committed branch checkpoints and agent-native session
anchors, never in-memory state between ticks.

### Other scheduler bindings

A persistent, single-host scheduler (such as a suitable cron, launchd, or systemd timer) may use
a detached launcher only when it targets one durable checkout and permits the detached executor to
survive after the trigger exits. It must not set `PRAUTO_AGENT`; the executor selects it.

An ephemeral CI scheduler or a multi-host scheduler must run the executor in the foreground, or
submit it to a durable worker. Such a binding must provide shared exclusion and persistent
continuity storage; a local PID lock and local native-session anchor alone do not coordinate
multiple hosts or survive an ephemeral workspace. Every binding preserves the contract: GitHub is
the phase-state SSOT, the evidence-based plan gate and generator ≠ reviewer are mandatory, and
cluster configuration remains anchored to `$REPO_DIR`.
