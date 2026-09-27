# langgraph_app — Zoho triage, through to the sprint-tasks issue

The LangGraph half of agentic-pager-duty. One `deepagents` graph, one thread
per Zoho ticket, cron-polled. Ends at the sprint-tasks issue; the GitHub
Action in `action/` takes over from there.

**Status:** skeleton, tests green, never run against live Zoho. Both kill
switches default to off, so a deployment cannot email a customer or open an
issue until someone deliberately turns them on.

Doing the first live run? Follow [`LIVERUN.md`](LIVERUN.md) — it is the
ordered checklist, including the one-ticket manual trigger that stands in
for the cron a local run cannot have.

```
zoho ticket ──poll──▶ thread ──▶ triage-analyst ──▶ /triage.md
                                      │
                    ┌─────────────────┴─────────────────┐
              query / k8s                          platform bug
                    │                                   │
               responder                           pager-scribe
                    │                                   │
              /draft_reply.md                     /pager_issue.md
                    │                                   │
            [GATE 1: zoho_send_reply]      [GATE 2: create_sprint_issue]
                    │                                   │
              reply to customer              sprint-tasks issue + label
                                                        │
                                              (GitHub Action takes over)
```

## The two gates

Both are `interrupt()` calls **inside the tool function body** — not prompt
instructions, and not `create_deep_agent(interrupt_on=...)`. The reasoning is
in `gates.py`; the short version is that a gate in configuration can be
mistyped or deleted silently, whereas the only code path to the side effect
runs through an `interrupt()` in the function that performs it.

Three properties the tests enforce:

| Property | Test |
|---|---|
| The side effect does not happen before the gate | `test_send_reply_interrupts_before_sending` asserts the fake Zoho client recorded **nothing** |
| No future side effect can be added above the gate | `test_interrupt_precedes_every_side_effect` parses the AST and compares line numbers |
| Only `{"approved": true}` is approval | `test_ambiguous_resume_never_sends`, parametrised over 12 shapes of garbage |

Run them: `python3 -m pytest tests/ -q` → 213 passing, no network.

### The subtlety that makes gates hard

`interrupt()` raises. On resume LangGraph **re-runs the tool from its first
line**, with the approval as the return value. So everything above the
`interrupt()` executes twice. Put a side effect there and you have built a
thing that acts twice and then asks permission.

This is also a second, independent route to double-replying a customer,
separate from the polling race: send succeeds → process dies before the
checkpoint commits → tool replays with approval in hand → second email.
`test_resume_replay_does_not_double_send` covers it.

## Not double-replying a paying customer

Four layers. Only one of them is a real mutex, and it is deliberately not the
one that looks like it.

1. **Deterministic thread id.** `uuid5(NS, "zoho-ticket:<id>")`. Two pollers
   compute the same id without coordinating.
2. **`threads.create(if_exists="raise")` — the actual mutex.** A primary-key
   insert. First caller wins, second gets 409 and moves on. Postgres decides.
3. **`multitask_strategy="reject"` on run creation.** The platform default is
   `enqueue`, which would *queue* a duplicate triage pass rather than refuse
   it. Wrong for this workload; overridden explicitly.
4. **Reply ledger at the send site.** Guards replay, which layers 1–3 do not
   touch.

**The store is not the lock, and cannot be.** `BaseStore.put` is
last-write-wins — no compare-and-set, no conditional put. A `get`-then-`put`
claim would look correct in tests and race in production;
`test_two_pollers_sharing_a_backend_start_each_ticket_once` is the test that
would have caught it.

**No watermark.** Zoho's search index lags behind writes (Zoho's own docs say
new resources "may require some time to be included in the index"), so a
strict `modified_since` watermark silently drops tickets. The query window
overlaps by 10 minutes and re-sees processed tickets every tick on purpose.
Re-seeing costs a 409; missing costs an ignored customer.

**Residual risk, stated plainly:** the gap between Zoho accepting the mail and
the ledger recording it. Small, not zero. Closing it properly means the
`draftReply` design below.

## Required secrets and their scopes

| Variable | What | Scope |
|---|---|---|
| `ANTHROPIC_API_KEY` | Model access | — |
| `ZOHO_CLIENT_ID` / `ZOHO_CLIENT_SECRET` | OAuth client | Self-client, per data centre |
| `ZOHO_REFRESH_TOKEN` | Long-lived | Minted once with `access_type=offline` **and** `prompt=consent` — without `prompt=consent` a re-auth silently returns no refresh token |
| `ZOHO_ORG_ID` | `orgId` header | Token is bound to one org; a mismatch is **403 `OAUTH_ORG_MISMATCH`**, not 401 — keep it out of the token-refresh path |
| `GITHUB_TOKEN` | Issue creation | `issues:write` on `devtron-labs/sprint-tasks` **only**. It does not need repo read, and must not have it |
| `ACP_ENDPOINT` | Agent Production Control Plane base URL | `https://autopilot-new.abhibhaw.com`. Never plain http, never `ACP_ALLOW_INSECURE_HTTP` |
| `ACP_CREDENTIAL` | Workload credential for discovery | Must grant agent key `pagerduty-triage`, or registration is a 403. From the secret store; never logged |
| `ACP_OTLP_TRACES_ENDPOINT` | Optional OTLP traces URL | `https://autopilot-new.abhibhaw.com/v1/traces` |
| `ACP_RELEASE_DIGEST` / `ACP_INSTANCE_KEY` | Optional | Release label on heartbeats (default: the platform's git SHA); per-replica key (default: hostname) |

Zoho OAuth scopes, minimum set:

```
Desk.tickets.READ,Desk.tickets.UPDATE,Desk.search.READ,Desk.basic.READ,Desk.channels.email.READ
```

Note `sendReply` needs `Desk.tickets.**UPDATE**` — not `WRITE`, not `CREATE`.
The wrong one is a 403 `SCOPE_MISMATCH`.

Rotation is unsolved — see open questions.

### Kill switches

`REPLIES_ENABLED` and `ISSUE_CREATION_ENABLED` default to `false`. With them
off the gates still fire and a human still approves, but the tool reports the
approval and stops instead of acting. This lets the whole pipeline run against
**production** Zoho with zero chance of a customer receiving agent-written
text. Turn them on one at a time.

## Running locally

```bash
cd langgraph_app
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

python3 -m pytest tests/ -q          # 213 tests, no network, no credentials

cp .env.example .env                 # then fill it in; never commit it
langgraph dev                        # http://localhost:2024
```

`ZOHO_TRANSPORT=fake` (the default) runs the whole graph against the in-memory
stub, so you can exercise both gates without touching Zoho.

## Control-plane discovery (autopilot)

`src/pagerduty_triage/acp.py` makes this deployment show up in the Agent
Production Control Plane's Agents inventory. Discovery is explicit — the
process registers with `ACP_CREDENTIAL` and heartbeats; nothing is patched or
auto-instrumented. The module docstring has the full reasoning; the short form:

* **Registers once per process** from the Starlette lifespan of the custom
  app, which the platform enters in the API server and in every queue worker.
  Registration runs on a daemon thread so it cannot delay readiness.
* **Heartbeats** once after registering, then every ~60 s from the SDK's own
  thread. A process that stops heartbeating shows stale after ~180 s.
* **One control-plane run per platform run** of `triage`, `poller` and
  `slack_notifier`, identified by the platform's run UUID only. A gate parking
  on `interrupt()` is a normal end; the resume is a new run.
* **Flushes** in the lifespan's shutdown half (the container stop hook).
* **Telemetry never crashes the agent.** Any failure logs a fixed message plus
  the exception type and the agent keeps running *unobserved* — so a clean
  process proves nothing. Look for `acp: registered workload_id=… instance_key=…`
  in the logs, then for the workload in the Agents list.

Nothing about a ticket — text, ids, subjects, customer — is sent; tenant and
environment come from the credential server-side.

`acp-sdk` is not on PyPI and the build cannot reach the private
`abhibhaw/autopilot` repo, so it is **vendored** in `vendor/acp-sdk/` and
listed as its own entry in `langgraph.json` `dependencies`. Its OpenTelemetry
pins are widened locally so it co-installs with `langgraph-api` — see
`vendor/acp-sdk/VENDORED.md` for provenance, the patch, and how to re-sync.

## Deploying

1. Push the repo; point LangGraph Platform at `langgraph_app/langgraph.json`.
2. Set every variable from `.env.example` as a deployment secret.
3. **Do not pass a checkpointer.** The managed runtime provisions Postgres
   persistence; a graph compiled with its own checkpointer will not use it,
   and the gates need persistence to park. `build_agent(checkpointer=None)` is
   the deployed path.
4. Create the cron. It **cannot** live in `langgraph.json` — it is an API
   call, and it needs Plus/Enterprise:

```python
from langgraph_sdk import get_client
client = get_client(url=LANGGRAPH_API_URL)

await client.crons.create(
    assistant_id="poller",
    schedule="*/2 * * * *",
    input={},
    on_run_completed="delete",   # tick threads are disposable; don't accumulate
)
```

## Answering a gate by hand (the fallback)

A parked thread waits indefinitely. Nothing times out, and no timeout ever
approves.

Find what is waiting:

```bash
curl -s "$LANGGRAPH_API_URL/threads/search" \
  -H 'content-type: application/json' \
  -d '{"status": "interrupted"}' | jq '.[].thread_id'
```

Read the request — this is the structured payload a Slack renderer will use:

```bash
curl -s "$LANGGRAPH_API_URL/threads/$THREAD_ID/state" \
  | jq '.tasks[].interrupts[].value'
```

```jsonc
{
  "version": 1,
  "gate": "zoho_send_reply",
  "ticket": { "id": "...", "subject": "...", "url": "https://desk.zoho.com/..." },
  "proposed_action": { "type": "customer_reply", "to": "...", "reply_text": "..." },
  "reasoning": "...",
  "warnings": ["..."],           // read these. "ALREADY SENT" appears here.
  "respond_with": { "approve": {"approved": true}, "reject": {"approved": false, "reason": "..."} }
}
```

Approve:

```bash
curl -s -XPOST "$LANGGRAPH_API_URL/threads/$THREAD_ID/runs" \
  -H 'content-type: application/json' \
  -d '{"assistant_id": "triage", "command": {"resume": {"approved": true}}}'
```

Reject with a reason — the agent revises and comes back:

```bash
  -d '{"assistant_id": "triage",
       "command": {"resume": {"approved": false,
                              "reason": "Too technical. Drop the stack trace."}}}'
```

Anything other than an explicit `{"approved": true}` is treated as rejection,
including `"yes"`, `true`, `{}`, and deepagents' own
`{"decisions":[{"type":"approve"}]}` envelope. Wiring a Slack button against
the wrong contract fails closed.

This path still works and is the fallback when Slack is down, when a gate
payload is too long to render, or when the renderer sees a payload version it
does not understand. It carries no identity, though — see the next section for
why that matters and what replaces it.

## Slack approval

The normal way to answer a gate. The API path above still works; this is the
one with an identity attached to it.

### Why it exists

A raw API resume carries no identity: anyone who can reach the endpoint can
approve. A Slack interaction carries a **verified user id**, which is what
makes an approver allowlist enforceable rather than aspirational. That is the
whole reason approvals moved to Slack — the buttons are a side effect.

Nothing about the graph changed. `gates.py` was already emitting a structured
dict so a renderer could turn it into blocks without re-parsing prose; the
Slack layer is that renderer plus a return path, and it sends back the same
`{"approved": …}` envelope the gate tools already accept.

```
thread parks on interrupt()
        │
   slack_notifier cron ── renderer ──▶ #pager-approvals: Approve / Reject
        │
   reviewer clicks
        │
   POST /slack/interactions ──▶ signing.py  (is this really Slack, and recent?)
        │                   └─▶ config.py   (is this the approver?)
        │
   resume.py ──▶ Command(resume={"approved": …, "approver": "U…"})
```

### Where the endpoint runs: on this deployment

LangGraph Platform hosts custom HTTP routes, so no separate service is needed.
`langgraph.json` carries:

```json
"http": { "app": "./src/pagerduty_triage/slack/http_app.py:app" }
```

Custom routes are **merged** into the platform's own router and take priority
over it, which is why the path is namespaced under `/slack/` where it cannot
shadow a system endpoint. The Request URL Slack needs is
`https://<your-deployment>/slack/interactions`.

**`http.enable_custom_route_auth` is left at its default of `false`, and that
is a decision, not an oversight.** With it false, custom routes bypass the
API-key auth that protects `/threads` and `/runs` — which is *required* here,
because Slack cannot present a LangGraph API key. The flag is also
all-or-nothing: turning it on would break this route along with every other
custom one, and per-route exemptions would mean branching on `path` inside
`@auth.authenticate`. That is more machinery for a check that still would not
tell us *which human* clicked.

So the endpoint is internet-reachable, and Slack's HMAC is the entire security
boundary. `signing.py` has no imports beyond the standard library for exactly
that reason, and `tests/test_slack_signing.py` plus
`tests/test_slack_handler.py` are the proof that it is shut.

`http_app.py` is a plain Starlette app with no LangGraph imports, so if that
endpoint ever has to be isolated from the deployment holding the Zoho and
GitHub credentials, `uvicorn pagerduty_triage.slack.http_app:app` runs it
standalone. That is a config change, not a rewrite.

### The properties, and the test that proves each

| Property | Enforced by | Test |
|---|---|---|
| Unsigned requests are rejected | `signing.verify_request`, called before the body is parsed | `test_unsigned_request_is_rejected_and_resumes_nothing` |
| Replayed / stale requests are rejected | timestamp inside the signed base string, ±5 min | `test_replayed_old_request_is_rejected`, `test_stale_request_is_rejected` |
| A tampered body is rejected | HMAC over the raw bytes | `test_tampered_body_is_rejected` |
| An unset signing secret closes the endpoint | explicit empty-secret branch that raises | `test_deployment_with_no_signing_secret_accepts_nothing` |
| Only the approver may approve | `SlackSettings.is_approver`, checked before any thread is touched | `test_non_approver_is_refused_visibly_and_resumes_nothing` |
| An empty allowlist means nobody | `is_approver` returns False on an empty set | `test_empty_allowlist_approves_nothing` |
| A double-click resumes once | pending-interrupt precondition in `resume.py` | `test_double_click_resumes_exactly_once` |
| Simultaneous clicks resume once | `multitask_strategy="reject"` → platform 409 | `test_two_reviewers_clicking_at_once_resume_once` |
| A rejection carries a reason | the reject menu has no reasonless option | `test_every_rejection_carries_an_actionable_reason` |
| A rejection never resumes with `None` | `platform._reject_none` | `test_a_rejection_never_resumes_with_none` |
| Nothing auto-approves | no timer anywhere in the package | `test_nothing_in_the_slack_package_can_auto_approve` |
| The reviewer sees the exact text | `rich_text_preformatted`, never summarised | `test_reply_text_is_shown_verbatim_and_unsummarised` |
| Credentials in ticket text are masked | `renderer.redact` | `test_credentials_pasted_into_a_ticket_are_masked` |
| The reviewer sees what the customer asked | `TicketContext` carries it; `renderer.customer_words_blocks` renders it | `test_the_customers_question_reaches_the_rendered_gate_message` |

### The customer's question is in the payload, not behind a link

Gate 1 asks a human to approve an *answer*. A subject line and a deep link are
not the question, and a reviewer who has to open a browser tab to check will
eventually stop checking. So the message shows the customer's own words,
verbatim, above the draft reply.

It could not be fetched inside `zoho_send_reply`: everything above
`interrupt()` runs before anyone sees the request and again on every resume,
and `test_interrupt_precedes_every_side_effect` fails any `deps.zoho.*` call
there — correctly. Instead **the poller reads it once, at claim time, and binds
it into the thread** through `TicketContext.description` /
`.customer_messages`. The gate tool already has it in hand.

Three things fall out of that:

* The conversation read is **best effort**. A ticket whose thread list fails
  still gets triaged on the description alone; refusing to start because a
  secondary read 404'd would be the wrong trade.
* It is **capped** — six inbound messages, 8 KB each — because it rides in
  `configurable` and therefore in every checkpoint of the thread.
* When anything is dropped, at either the poller or the renderer, the message
  says so. `MAX_QUESTION_BLOCKS` / `MAX_FOLLOWUP_BLOCKS` bound the Slack side;
  the reply itself is never the thing squeezed, because the reply is what is
  being approved.

If the words are missing entirely, that is a wiring failure, and both the gate
payload (a warning) and the Slack message say so rather than showing a blank.

### Exactly once, honestly labelled

Three layers, and only the first two are enforcement:

1. **The pending-interrupt precondition.** Before resuming, the handler reads
   the thread state and requires that it is still parked on the *same*
   interrupt id the button was rendered for. The first click consumes that
   interrupt, so a late second click fails the precondition.
2. **`multitask_strategy="reject"`.** Two clicks in the same second can both
   pass the precondition — the read is not atomic with the write. The platform
   refuses a second run while one is in flight, and that refusal is a database
   decision, not ours. Same mechanism `poller.py` relies on.
3. **The ledger guards inside the gated tools.** Even if both failed,
   `zoho_send_reply` re-checks `ledger.reply_record` after the gate. A double
   resume costs a wasted model turn, not a second email.

**The store is not the lock**, here or anywhere else in this project:
`BaseStore.put` is last-write-wins with no compare-and-set. The notifier's
"already posted" record is a *record*, not a claim — two racing ticks post the
gate twice, and the first click on either message consumes the interrupt, so
the duplicate is a duplicate **message**, never a duplicate action.

### Rejection is a menu, not free text

Slack buttons cannot collect text, and a free-text reason needs a modal. A
modal needs a `trigger_id`, which Slack documents as **single-use and valid for
three seconds**, so `views.open` must happen synchronously inside the button
handler, and a whole second payload type (`view_submission`) has to be parsed,
verified and routed.

Instead the Reject control is a select menu of five canned reasons per gate,
each a full instruction the agent can act on ("Wrong tone, or too technical for
this customer. Rewrite it plainly, and do not include stack traces…"). There is
deliberately **no reasonless reject option**: `gates.py` turns a reasonless
rejection into "rejected without a reason", and the agent cannot revise against
that. Free text is the obvious v1.1 upgrade if the canned set proves too blunt.

### An unanswered gate parks forever

There is no reminder, no nag, no escalation and no timeout anywhere in the
package — `test_nothing_in_the_slack_package_can_auto_approve` greps for
`sleep(`, `threading.Timer`, `schedule(` and `auto_approve` and fails if any
appears. If a reminder is ever added it must post a *new message* and must
never resume a thread.

### Setting the app up

1. **Create the app** at <https://api.slack.com/apps> → *From scratch*, in the
   Devtron workspace.
2. **OAuth & Permissions → Bot Token Scopes:** add **`chat:write`**. That is
   the only scope needed. The interaction handler uses `response_url`, which
   carries its own authorization, so it needs no token at all — only the
   notifier holds the bot token. Do not add `chat:write.public`; the bot should
   be a member of one private channel, not able to post anywhere.
3. **Install to Workspace.** Copy the Bot User OAuth Token (`xoxb-…`) into
   `SLACK_BOT_TOKEN`.
4. **Basic Information → App Credentials → Signing Secret** → into
   `SLACK_SIGNING_SECRET`. This is not the bot token and not the deprecated
   verification token.
5. **Create a private channel** (`#pager-approvals`), invite the bot, and put
   its channel **id** (`C…`, from *View channel details*) into
   `SLACK_CHANNEL_ID`. Private matters: gate messages carry verbatim customer
   ticket text.
6. **Interactivity & Shortcuts → on**, Request URL
   `https://<your-deployment>/slack/interactions`. Slack sends a test POST;
   the route answers it, and an unsigned probe gets a 401.
7. **Find your own user id** — Slack profile → ⋮ → *Copy member ID* (`U…`) —
   and set `SLACK_APPROVER_USER_ID`. **Nothing can be approved until this is
   set**, which is the intended failure direction.
8. **Create the notifier cron.** Same API call as the poller's, same
   Plus/Enterprise requirement:

```python
await client.crons.create(
    assistant_id="slack_notifier",
    schedule="*/2 * * * *",
    input={},
    on_run_completed="delete",
)
```

Check it with `GET https://<your-deployment>/slack/health` — it reports which
variables are unset, and never their values.

### What is deliberately not built

* **No free-text rejection.** See above.
* **No workspace check.** The app is installed in one workspace, and the HMAC
  already proves the request came from our app. A second install elsewhere
  would need `team.id` checked as well.
* **No free-text customer context beyond the carried thread.** Gate 1 now
  carries the customer's words — see below — but capped: the description plus
  the last six inbound messages, 8 KB each. A longer conversation is shown
  shortened, with a banner saying so and a link to the full ticket. Raising the
  cap costs checkpoint size on every thread, so it stays until something needs
  it.


## Zoho Desk: no usable MCP server exists

This was an open question in `CLAUDE.md`. It is now answered: **no**. Evidence:

* **Zoho's own MCP** (`zoho.com/mcp`, GA March 2026, free) lists Desk among 46
  supported services — but there is no *dedicated* Desk MCP server, only a
  low-code console at `mcp.zoho.com` where you hand-pick tools. Zoho's GitHub
  org ships MCP servers for Analytics, Apptics and Billing; **not Desk**. The
  Desk tool catalogue is not publicly enumerable, so I could not confirm
  whether `sendReply` or thread-content reads are even offered. Its headless
  auth mode is a shared server URL with an embedded key — an unscoped bearer
  credential for a server that can email customers.
* **Community:** the official MCP registry has zero Desk servers. npm: zero.
  PyPI: zero. Smithery has one undeployed stub. GitHub search returns 7 repos;
  the best two are a 3-star one-day project with no token refresh, and a
  2-star agency side project with no licence and no tests.
* **Klavis AI's Zoho Desk docs page is a false positive** — the
  `mcp_servers/zoho_desk` directory it points at does not exist in their repo.
* **Aggregators split the loop:** Zapier MCP can send a reply but exposes no
  conversation read; Composio can read conversations but has no reply tool.
  Neither exposes attachment download, and both put a third-party SaaS in the
  path of customer email.

So: a thin MCP wrapper over Zoho Desk REST v1, scoped to the five operations
this agent needs. `ZOHO_TRANSPORT` switches between `rest`, `mcp` and `fake`
behind the `ZohoDeskClient` protocol, so nothing above the seam changes.

**Build it from Zoho's OpenAPI spec, not by hand.** Zoho publishes a
first-party OAS 3.1 for Desk at `github.com/zoho/zohodesk-oas` (186 module
files, actively updated). `Ticket.json`, `Thread.json`,
`TicketAttachment.json` and `Search.json` are the four that matter.

## What is not proven

Honest list. None of this is blocked — it is just not done.

* **The graph has never been *run*, but it now builds.** `deepagents` is
  installed, so `tests/test_agent_wiring.py` compiles the real graph and reads
  the tool registry off it. What is still unexercised is an actual model turn:
  no LLM call has been made. Everything tested is the wiring plus the logic
  around it — gates, tools, ledger, poller, template.
* **Classification accuracy is unmeasured.** The tests prove routing *plumbing*
  and that the owner-confirmed routing facts reach the prompt. Whether the
  model classifies correctly needs an eval over the 135-ticket corpus in
  `data/corpus.json`, with the same leave-one-out discipline `CLAUDE.md`
  already mandates for localization.
* ~~**Two integration checks are marked `TODO(verify on first deploy)`**~~
  — both resolved against deepagents 0.7.14 the first time the deps were
  installed:
  * Passing a `FilesystemMiddleware` **replaces** the default rather than
    adding a second. `deepagents.graph._apply_custom_middleware` merges by
    `.name`. The `execute` shell is genuinely gone from the compiled graph,
    main agent and all four subagents.
  * Subagent `tools` **does not** accept tool-name strings. It takes `BaseTool`
    objects; a string reaches `create_tool()`, which returns the decorator
    function, and `ToolNode` then fails with `'function' object has no
    attribute 'name'`. Subagents now pass `tools: []` and get their filesystem
    tools from their own `FilesystemMiddleware`.
  * Turned up by the same run, and not anticipated by either note:
    `create_deep_agent` auto-adds a `general-purpose` subagent that inherits
    the main agent's tools, which handed it `zoho_send_reply` and
    `create_sprint_issue`. `_general_purpose_override()` supplies an explicit
    spec with `tools: []` instead.
* **`zoho/rest.py` is written but has never been run.** Every endpoint was
  taken from Zoho's published spec rather than from memory, and no call in it
  has ever met a real Zoho server. `zoho/mcp.py` is still a stub —
  `ZOHO_TRANSPORT=mcp` will not work.

## Known gaps

* **Follow-up customer messages are dropped.** One thread per ticket means a
  second message on an already-answered ticket has no live thread. Needs an
  owner decision on what a second reply should even do.
* **No sweeper for stalled threads.** A crash between claiming a thread and
  starting its run leaves a ticket that looks started and is not. The ledger
  records `seen_at` so a sweeper can find these; the sweeper is not built.
* ~~**`execute` may still be reachable.**~~ Checked on the compiled graph, not
  asserted: `test_no_shell_tool` reads the `ToolNode` registry of the main
  agent and of every compiled subagent, and fails if the filesystem middleware
  is built without its `tools=` restriction.

## Open questions for the owner

1. **Webhooks instead of cron?** Zoho Desk supports `Ticket_Add`,
   `Ticket_Update` and `Ticket_Thread_Add` webhooks. A 2-minute cron burns
   ~2,160 API credits/day before reading a single ticket, and inherits the
   search-index lag the overlap window exists to paper over. Webhooks remove
   both. The cost is a public authenticated ingress, which the design
   explicitly avoided. Worth revisiting now that the index-lag problem is
   known — it was not, when that call was made.

2. **Should gate 1 write a Zoho *draft* instead of holding text in a thread?**
   Zoho supports `draftReply` / `sendDraft`. Writing the draft first, then
   interrupting, then resuming into `sendDraft` would mean: the reviewer sees
   the reply in the Desk UI with native send/delete buttons, a rejected draft
   costs nothing, and a parked thread no longer holds the only copy of unsent
   text. Strictly better failure modes.
   **The catch:** draft creation is a side effect, and everything above
   `interrupt()` runs twice — so a naive version creates two drafts per
   resume. It needs `PATCH draftReply` on replay, which is a real design, not
   a tweak. Recommended for v1.1; deliberately not smuggled into v1.

3. **Credential rotation.** Zoho refresh tokens do not expire but are evicted
   at **20 active per client per user**; the GitHub PAT does expire. Nothing
   here rotates either, and the failure mode is silent until a poll 401s.

4. **Is `Open` the right status filter?** `ZOHO_POLL_STATUSES` defaults to
   `Open`. If the team uses a custom first-touch status, tickets will be
   invisible to the poller and nobody will notice.

5. ~~**Who may approve a gate?**~~ **Answered by Slack.** A verified Slack
   user id is checked against `SLACK_APPROVER_USER_ID` before any thread is
   resumed, and the id is recorded into the thread via `GateDecision.extra`.
   Shivam still has to supply the id — until he does, nothing can be approved
   from Slack, which is the safe direction.

   The residual: the raw API path still has no identity. It is documented as
   the fallback for a Slack outage, and anyone who can reach `/threads` can
   still use it. Closing that means platform-level auth on the API, not more
   code here.
