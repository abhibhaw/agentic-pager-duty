"""The poller as a one-node graph, so a LangGraph cron can target it.

A cron on LangGraph Platform triggers an **assistant**, not a function, so the
polling loop needs to be a graph. This is that wrapper and nothing more: all
the logic lives in `poller.py`, which is plain Python and therefore testable
without a platform.

The cron is created against this assistant *threadlessly*, which makes the
platform open a fresh thread per tick. Those threads are disposable — the
durable state is the per-ticket triage threads this poller creates, plus the
store. Create the cron with `on_run_completed="delete"` so ticks do not
accumulate (see README).
"""

from __future__ import annotations

import os
from urllib.parse import quote
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from pagerduty_triage.acp import observe
from pagerduty_triage.agent import build_deps
from pagerduty_triage.poller import ThreadConflict, poll_once
from pagerduty_triage.tools.deps import TICKET_CONTEXT_KEY


class PollerState(TypedDict, total=False):
    """Trivial state: the cron sends nothing, the node returns a report."""

    report: str
    started: list[str]
    errors: list[str]


def _platform_client():
    """A `LangGraphClientLike` backed by the real SDK.

    Imported lazily so that importing this module — which tests do — does not
    require the SDK or a reachable platform.

    ## Why the *sync* client, specifically

    ``_start_one`` makes two calls on one client — the claim, then the run —
    and they must share a live connection. The async client wrapped in a
    per-call ``asyncio.run()`` cannot: ``asyncio.run`` closes its loop on the
    way out, while the shared ``httpx.AsyncClient`` keeps the keep-alive
    connection created on that loop in its pool. The second call picks that
    connection up and awaits a transport bound to a dead loop, which surfaces
    as ``RuntimeError('Event loop is closed')`` — after the thread was already
    created. A claimed ticket with no run behind it is the one failure this
    module must not produce.

    Owning a long-lived loop here would fix the symptom and leave a loop to
    leak per call site. ``get_sync_client`` removes the loop from the problem:
    it is ``httpx.Client``, safe both here (a plain CLI process) and in
    ``poll_node``, which LangGraph runs in a worker thread. It is also what
    ``slack/platform.py`` already uses against the same API.
    """
    from langgraph_sdk import get_sync_client

    raw = get_sync_client(url=os.environ.get("LANGGRAPH_API_URL"))

    class PlatformClient:
        def create_thread(self, thread_id, *, metadata, if_exists):
            try:
                return raw.threads.create(
                    thread_id=thread_id, metadata=metadata, if_exists=if_exists
                )
            except Exception as exc:  # noqa: BLE001
                # The SDK raises an httpx status error; 409 means someone else
                # already owns this ticket. Translating it here keeps the
                # poller free of HTTP concerns.
                if _is_conflict(exc):
                    raise ThreadConflict(str(exc)) from exc
                raise

        def bind_ticket_context(self, thread_id, *, ticket_context):
            # `patch` semantics: the claim metadata written by create_thread
            # (zoho_ticket_id, ticket_number, subject) must survive, so this
            # adds a key rather than replacing the mapping.
            return raw.threads.update(
                thread_id, metadata={TICKET_CONTEXT_KEY: ticket_context}
            )

        def create_run(
            self, thread_id, *, assistant_id, input, config, multitask_strategy
        ):
            return raw.runs.create(
                thread_id,
                assistant_id,
                input=input,
                config=config,
                multitask_strategy=multitask_strategy,
                webhook=run_finished_webhook(),
            )

    return PlatformClient()


def run_finished_webhook() -> str | None:
    """Where the platform should call back when this run ends.

    A run that parks on `interrupt()` *ends*, so the callback fires and the gate
    card is posted seconds later. This is what removes the notifier cron.

    Returns None when either variable is unset, which is the correct local
    default: `langgraph dev` has no public URL, so there is nothing for the
    platform to call, and the notifier is ticked by hand. Passing None is the
    same as not passing it at all.

    The secret is in the query string because the platform does not sign its
    callbacks and the route bypasses API-key auth. See `slack/http_app.py`.
    """
    base = os.environ.get("PAGER_PUBLIC_URL", "").rstrip("/")
    secret = os.environ.get("PAGER_WEBHOOK_SECRET", "")
    if not base or not secret:
        return None
    from pagerduty_triage.slack.http_app import RUN_FINISHED_PATH

    return f"{base}{RUN_FINISHED_PATH}?token={quote(secret, safe='')}"


def _is_conflict(exc: Exception) -> bool:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status == 409 or "409" in str(exc)


def poll_node(state: PollerState) -> PollerState:
    deps = build_deps()
    report = poll_once(
        zoho=deps.zoho,
        ledger=deps.ledger,
        client=_platform_client(),
        settings=deps.settings,
        assistant_id=os.environ.get("TRIAGE_ASSISTANT_ID", "triage"),
    )
    return {
        "report": report.summary(),
        "started": report.started,
        "errors": report.errors,
    }


def make_poller_graph():
    builder = StateGraph(PollerState)
    builder.add_node("poll", poll_node)
    builder.add_edge(START, "poll")
    builder.add_edge("poll", END)
    return observe(builder.compile())
