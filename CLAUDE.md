# agentic-pager-duty

Automates Devtron's pager-duty flow: Zoho Desk ticket → triage → sprint-tasks issue
→ localize → fix → build/test → draft PR. Humans keep two decisions (what is said to
a customer, and whether something is really a platform bug) and the merge.

**Status:** design approved. Localization harness built and verified. The two agent
halves — GitHub Action fix engine (`action/`) and LangGraph Zoho triage
(`langgraph_app/`) — are in active development.

**Deferred deliberately:** the localization baseline measurement (harness plan Tasks 5
and 8). The harness works and the corpus is sound, but running the numbers was parked to
focus on the agents themselves. The past-PR index is an accelerant that can be optimised
incrementally; it is not a prerequisite for the workflow. Resume from
`tools/eval/run_eval.py` when the agents are further along — and honour the eval
invariants below, or the number will be fake.

- Design: [`docs/plans/2026-09-16-agentic-pager-duty-design.md`](docs/plans/2026-09-16-agentic-pager-duty-design.md)
- Phase 1–2 plan: [`docs/plans/2026-09-16-phase-1-2-localization-harness.md`](docs/plans/2026-09-16-phase-1-2-localization-harness.md)
- Reference ticket: [sprint-tasks#2960](https://github.com/devtron-labs/sprint-tasks/issues/2960)

## Eval invariants (violate these and the number is fake)

- **Build the index leave-one-out.** The past-PR index is mined from the same closed
  tickets the eval replays. Verified: drop ticket 2960 and all six RBAC entries vanish —
  they *are* 2960's answer. Always
  `build_index([r for r in corpus if r["number"] != ticket_under_test])`.
- **Redact the ticket body before prompting.** 119 of 135 issue bodies embed their own
  fix PR URLs, because the PR template is pasted into the issue *after* the fix ships.
  They were never there when a developer picked the ticket up, so redacting restores the
  correct point-in-time state. `replay.py` cuts at `## PR Links` and strips residual
  GitHub URLs.
- **Exclude structurally unlocalizable tickets** rather than averaging them in as zeros:
  tickets whose only fix PRs are in `protos` or `hotfix-migrations` (not cloned), and
  records with `n_true == 0`.
- **A shared-code fix needs both repos.** 2960's four paths appear in `devtron` and
  `devtron-enterprise`, so `n_true` is 8 and finding only the OSS half scores 0.5. That
  is correct — half the job is half the score. Confirmed by Shivam.

## Measurements

**Corpus (2026-09-16):** 135 tickets from 222 closed `pager-duty` issues, 288 fix PRs,
2,292 ground-truth files. 21 distinct affected areas; 19 tickets carry no area. 87
skipped for having no linked fix PR; 7 carry `unreadable_prs` (the deleted/inaccessible
`devtron-fe-lib`).

Localization not measured yet. Phase 1–2 Task 5 records the cold localization baseline here,
and Task 8 records the same tickets re-run with the past-PR index. Every later change
is judged against those two numbers.

## Working agreements

- **Never commit or push.** Shivam reviews and commits everything himself, or says
  so explicitly. Write and edit freely, then stop and hand it over for review.
- **Keep this file current.** When a decision changes or a component lands, update
  CLAUDE.md in the same batch of edits.
- **Don't write API call shapes from memory.** The Claude Code GitHub Action, the
  Agent SDK, and LangGraph surfaces all move. Read current docs at implementation
  time. The `claude-api` skill is the source of truth for Anthropic APIs.

## Architecture

Two systems joined at one seam — the sprint-tasks issue, which is already the
handoff in the human process.

**LangGraph Platform (Python, managed)** — the Zoho brain and the gates.
A single `deepagents` graph, one thread per Zoho ticket, cron-polled every ~2
minutes. Subagents: `triage-analyst` (classify), `responder` (draft customer
reply), `pager-scribe` (fill the pager template). They pass work through the
virtual filesystem, not through the main agent's context.

**GitHub Actions (`devtron-labs/sprint-tasks`)** — the fix engine, in `action/`.
Triggered on `issues.labeled: agent-fix`. **One self-directed `claude` CLI session**
that localizes, explains, fixes and reports; `bin/publish.sh` then opens the draft PRs
in a separate step. Drives the CLI directly rather than `anthropics/claude-code-action`
because that action's token is scoped to its trigger repo only (confirmed in its
`action.yml` and security docs), so it cannot open PRs in the eight code repos.

**Collapsed from four stages to one on 2026-09-17**, deliberately. The old
`01-localize → 02-rca → 03-fix → 04-review` chain justified itself as "gates are harness
properties, not prompt instructions" — but `jq -r .can_explain` reads a field *the agent
wrote about itself*. It was a structured self-report, not independent verification, and
buying it cost a second full read of eight repos: localization's exploration was
discarded and the RCA rediscovered it from a JSON summary. What actually prevents a bad
fix is unchanged and is listed in the invariants below.

**No build step.** The two-tier clone existed because localization ran first and named
the repos worth vendoring; one session picks its repos mid-run, so there is nothing to
target and materialising all eight costs ~1.8 GB. The draft PR's own CI compiles the
change, later, for free, where the reviewer is already looking. `go build` passing was
never evidence of correctness here. The agent is told not to compile and to report
`build.ok` false, which the PR body prints as "This change has not been compiled."

**Clone strategy:** `workspace/` measures 2,511 MB but `vendor/` is 71% of it and actual
searchable source is ~150 MB. All eight are cloned sparse (no vendor/docs/assets).

Chosen over a hosted sandbox specifically because no enterprise source moves
anywhere new: the Action runs where the code and credentials already are, and
LangGraph Platform only ever sees ticket text and its own state.

## Invariants

- **Gates live inside tools, never in the prompt.** `zoho_send_reply` and
  `create_sprint_issue` each call `interrupt()`. A single deep agent picks its own
  route, so a gate that is only an instruction is a gate it can walk past.
- **An unanswered gate never becomes an implicit yes.** Threads park indefinitely;
  no timeout auto-approves.
- **Agent PRs are always `draft` and labelled `agent-authored`.** A wrong auth fix must
  not be able to become a merged auth change. **Correction:** "the token has no merge
  permission" is *not achievable by token scope* — fine-grained PATs have no separate
  merge permission, and `pull_requests: write` grants create *and* merge. What actually
  enforces this is (a) `--draft`, since a draft PR cannot be merged, and (b) branch
  protection on `main` in all eight repos — **confirmed already in place** by Shivam.
- **Write credentials never sit in the same process as the agent.** The fix stages run
  with a read-only token; publishing is a separate step with a separate write token.
- **The agent cannot open its own PR, and this is the load-bearing one.** The session
  holds no GitHub token and `gh` is denied in `settings/fix.json`, so it cannot publish
  however it decides to proceed. `publish.sh` runs in a separate workflow step with a
  separate write-scoped token the agent never sees. Removed in the collapse: the
  separate adversarial-review stage. `claude-code-review.yml` reviews opened PRs with
  fresh context, which beats a stage re-reading its own session's reasoning — **but it
  is currently installed only in `devtron-enterprise`**, so a fix landing in the other
  seven repos gets no automated review at all.
- **The fix engine triggers on `agent-fix`, never `pager-duty`.** `pager-duty` is applied
  to every pager issue by the normal human process, so triggering on it would run the
  agent on everything ever filed. **The triage agent applies `agent-fix` itself at issue
  creation** (`sprint_tasks.FIX_LABEL`) — gate 2 is already the authorisation, and asking
  the reviewer to then click a label is the same decision twice. The label is in the gate
  payload, so it renders in the Slack card *before* the approval. A human can still add
  it by hand to any pager issue the agent never touched.
- **No speculative PRs.** If the agent cannot explain the defect or cannot write a fix it
  would defend, it reports `outcome: stopped`; the harness comments the analysis and
  opens nothing. This is now a prompt instruction rather than a `jq` test — see the
  architecture note above for why that is a smaller change than it sounds.

## Target repositories

A bug can be in any of these, so localization is part of the problem, not an input:

`devtron` · `devtron-enterprise` · `dashboard` · `devtron-services` ·
`devtron-services-enterprise` · `athena-be` · `notifier` · `devtron-fe-common-lib`

Fixes may span two repos; open a draft PR in each and cross-link on the issue.

**`devtron-enterprise` is a hard fork of `devtron`, not a dependency** — both declare
the same Go module path, kept in sync by a merge workflow. 12 of 13 `devtron` tickets
also touched enterprise, so a shared-code fix needs two near-identical PRs. The
`*_ent.go` files (48 in OSS, 157 in enterprise) sit at *identical paths with divergent
bodies* — same function, different signatures — so a patch written against one repo
will not apply to the other. Each repo needs its own diff.

`devtron-services` / `-enterprise` are multi-module monorepos with no top-level
`go.mod`; the enterprise services are thin wrappers that `replace` to their OSS
counterparts. `dashboard` depends on `@devtron-labs/devtron-fe-common-lib`, which is in scope —
many UI bugs land there rather than in `dashboard` itself.

**The reviewer is the gate, not the test suite.** Confirmed by Shivam: correctness here
is established by a logical reviewer reasoning about the change, not by CI going green —
which matches the build traps below. Taken to its conclusion on 2026-09-17: **the agent
does not build at all.** A compile check that proves nothing about correctness is not
worth ~1.8 GB of vendoring per run, and the draft PR's own CI performs it anyway, in the
place the reviewer is already reading. What the agent must produce is a *reviewable*
change: the causal chain from reported symptom to the specific line, what was verified
and what was not, and a verification plan a human can execute. The stopping condition is
not "tests failed" but "cannot explain why this fix is correct" — the better gate for
RBAC bugs, where a wrong fix compiles and passes everything.

**Build traps:** `devtron`'s `make test-unit` runs only `go test ./pkg/pipeline`;
`dashboard` CI never runs `yarn test`; `make build` needs a `wire` binary that is not
vendored. All Go modules vendor their deps, so builds work offline.

**Policies tickets are enterprise-only — one PR, not two.** OSS `pkg/policyGovernance/`
holds only `security/`; approval config, artifact promotion, lock configuration and
deployment windows live solely in `devtron-enterprise`. This is the standing exception
to the two-PR rule. Confirmed by Shivam.

**"Security issue (secrets leak/log/visible)" means secret *handling*, not image-scan
findings** — route to `devtron-services/common-lib/securestore/` and
`devtron/pkg/pipeline/ConfigMapService.go`. Confirmed by Shivam.

**Pager fixes target `main`** in every repo, including `dashboard` (whose default
branch is `develop`). The Action branches from `main`.

## Build order

1. ✅ **Localization harness.** `tools/corpus/` mines 135 closed pager tickets into
   `data/corpus.json` (288 fix PRs, 2,292 ground-truth files); `tools/eval/score.py`
   scores a predicted file list against it; `tools/eval/replay.py` drives Claude Code
   headless over the eight clones. `context/repo-map.md` and `context/pager-index.md`
   are the context artifacts the agents consume.
2. 🔄 **The two agent halves, in parallel** — `action/` (GitHub Action fix engine) and
   `langgraph_app/` (LangGraph Zoho triage through issue creation, both gates live).
3. ⏸ **Measure localization** — baseline, then with the index. Deferred, not cancelled.
4. **Wire the seam**, and the PR link back to Zoho.

## Open questions

- ~~Is there a usable Zoho Desk MCP server?~~ **ANSWERED: no.** Official registry, npm,
  and PyPI all have zero. The best GitHub options are a 3-star project with no token
  refresh and a 2-star one with no licence or tests. Zoho's own MCP is GA and lists Desk
  but ships no dedicated Desk server. Aggregators split the loop — Zapier can reply but
  not read conversations, Composio can read but not reply. So `langgraph_app/mcp_server/`
  is a thin five-tool wrapper built on Zoho's first-party OpenAPI spec
  (`github.com/zoho/zohodesk-oas`).
- ~~Who may approve a gate?~~ **Being answered by Slack.** A raw API resume carries no
  identity; a Slack interaction carries a verified user id, so approval becomes an
  allowlist of Slack user ids in a scoped channel. Shivam still needs to supply the
  allowlist.
- **Cron vs webhooks for Zoho polling.** The 2-min cron burns ~2,160 LangGraph credits/day
  before reading anything, and Zoho's search index lags writes — a fact not known when the
  polling decision was made. Worth revisiting.
- Credential storage and rotation: Zoho OAuth, a GitHub PAT covering all seven
  repos including the private enterprise ones, and an Anthropic key.

## Library facts established the hard way

- **`deepagents` 0.7.14:** `write_todos` is *not* in the default middleware stack — pass
  `TodoListMiddleware` explicitly or the prompt tells the model to call a tool that does
  not exist. The filesystem middleware ships an `execute` shell tool, which a triage agent
  reading attacker-influenced customer text must not hold; it is stripped, and a test
  fails if the strip stops working. Subagent key is `system_prompt` — a `prompt` key is
  silently ignored, yielding a subagent with no instructions.
- **LangGraph Platform:** do not pass a checkpointer (the managed runtime provisions one).
  `multitask_strategy` defaults to `enqueue`, which is wrong here. Crons cannot live in
  `langgraph.json` — they need an API call and a Plus/Enterprise plan.
- **The store is not a mutex.** `BaseStore.put` is last-write-wins with no compare-and-set,
  so a get-then-put claim passes tests and races in production. The real mutex for
  once-per-ticket processing is `threads.create(if_exists="raise")` — a primary-key insert
  where Postgres decides.
- **Zoho scopes:** `sendReply` needs `Desk.tickets.UPDATE`, not WRITE.

## Known risk

Unattended draft PRs on Severity-1 RBAC bugs in infrastructure software: a
plausible-but-wrong auth fix is a security hole that passes a green build. Raised
and accepted. Mitigations are the invariants above plus human review at merge.

## Control-plane discovery (autopilot) — BUILT, not yet verified in prod

`langgraph_app/src/pagerduty_triage/acp.py`, tests in `tests/test_acp.py`. Registers the
LangGraph deployment with the Agent Production Control Plane as agent key
**`pagerduty-triage`** (framework `langgraph`) so it appears in the Agents inventory.
`acp-sdk` 0.1.0 is **vendored** in `langgraph_app/vendor/acp-sdk/` from autopilot commit
`33aefa6` (tip of `v2`), because the image build has no SSH access to the private repo. It
is its own entry in `langgraph.json` `dependencies`. `src/` is byte-identical to upstream;
`pyproject.toml` carries one real patch — **OTel pins widened from `==1.44.0` to
`>=1.42.1,<1.45`**, because upstream's pin cannot co-install with `langgraph-api` 0.15.x
(`opentelemetry-sdk<1.43`) and the image would not build. The SDK's own 93 tests pass on
both 1.42.1 and 1.44.0. Provenance and re-sync steps: `vendor/acp-sdk/VENDORED.md`.

- **Registration hangs off the custom app's Starlette lifespan**, which `langgraph-api`
  enters in the API server *and* every queue worker (read from its source, 0.15.1). It
  runs on a daemon thread: the platform warns at 10 s of lifespan startup and fails
  readiness at 30 s, and the SDK's register retries can outlast that.
- **Runs are a root-run callback** (`observe()` → `graph.with_config(callbacks=…)`) on all
  three graphs, keyed by the platform run UUID. Verified: a run that parks on
  `interrupt()` fires `on_chain_end`, not an error, so parking is `succeeded` and the
  resume is a new run.
- **Telemetry never crashes the agent** — failures log a fixed message plus the exception
  *type*, never its text. A clean process therefore proves nothing; the evidence is the
  `acp: registered workload_id=…` log line and the workload in the Agents list.
- Never sent: ticket text, ids, subjects, customer identifiers, tenant, environment.
  `execute_tool`/`get_action`/`resume_action` are deliberately unused.

## Slack approval for the human gates — BUILT

Lives in `langgraph_app/src/pagerduty_triage/slack/`, 179 tests, no `slack_sdk`
dependency (stdlib `hmac` + `urllib`).

**Deployment:** a Starlette app mounted via `http.app` in `langgraph.json`, namespaced
under `/slack/`. Routes merge into the platform router rather than sub-mounting, so the
namespace prevents shadowing a system route. No separate service to run.

**`http.enable_custom_route_auth` is left at its default `false` — a stated decision, not
an oversight.** Custom routes then bypass the API-key auth protecting `/threads` and
`/runs`, which is *required*, because Slack cannot present a LangGraph key. The
consequence: the endpoint is internet-reachable and **Slack's HMAC signature is the
entire security boundary.** Verification runs before the body is ever parsed, an unset
signing secret closes the endpoint rather than opening it, and timestamps are checked
±5 min in both directions.

**One approver**, `SLACK_APPROVER_USER_ID`. An empty allowlist approves nobody. Slack
scope is `chat:write` only — the interaction handler holds no Slack token at all, using
`response_url`, which carries its own authorization.

**Exactly-once** comes from the pending-interrupt precondition (the first click consumes
it) plus `multitask_strategy="reject"`, whose 409 is treated as a loss rather than
mistaken for success.

**Known gap to close before production Zoho:** the gate payload carries the ticket
subject and link but **not the customer's original question**, so a reviewer approves a
reply without seeing what was asked. The fix is to thread the description through
`TicketContext` from the poller — *not* to call `deps.zoho.get_ticket()` inside the gate
tool, which would put a side effect above `interrupt()` and rightly fail
`test_interrupt_precedes_every_side_effect`.

**Still identity-free:** the raw LangGraph API resume path. Anyone who can reach
`/threads` can still approve. Closing that is platform-level auth, not more code. Gate requests surface in a Slack channel where the reviewer sees
the ticket context and the exact action proposed (the customer reply text, or the
classification plus filled pager template), and approves or rejects inline.

This is a **presentation layer over the existing `interrupt()`**, not an architecture
change — the graph is unchanged, a renderer turns a pending interrupt into a Slack
message, and the approve/reject click resumes the LangGraph thread.

Two things to get right in v1 so this doesn't need rework:

- **Make the interrupt payload structured, not prose.** A dict — ticket id, gate type,
  the proposed action verbatim, the reasoning, and links back to the Zoho ticket — is
  directly renderable into Slack blocks. A prose string would have to be re-parsed.
- **A Slack click is an authorization decision.** Gate 1 sends text to a paying
  customer; gate 2 starts an unattended chain that ends in an open PR. Scope the
  channel and check approver identity — anyone who can see the message can otherwise
  click approve.

Rejection needs a path too: a reject should carry a reason back into the thread so the
agent can revise rather than just halting.
