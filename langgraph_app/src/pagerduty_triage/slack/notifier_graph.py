"""The notifier as a one-node graph, so a LangGraph cron can target it.

Same wrapper pattern as ``poller_graph.py`` and for the same reason: a cron on
LangGraph Platform triggers an *assistant*, not a function. All the logic is
in ``notifier.py``, which is plain Python and testable without a platform.

It is a separate assistant from ``poller`` rather than an extra step inside
the poll tick, so that a Slack outage cannot stop Zoho tickets being picked
up, and so neither can be deployed without the other working. If the second
cron ever becomes an annoyance, folding this call into ``poll_node`` is a
two-line change.
"""

from __future__ import annotations

import os
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from pagerduty_triage.acp import observe
from pagerduty_triage.slack.config import load_slack_settings
from pagerduty_triage.slack.notifier import notify_parked_gates


class NotifierState(TypedDict, total=False):
    report: str
    posted: list[str]
    errors: list[str]


def _store() -> Any:
    """The platform-injected store, or an in-memory one locally.

    The in-memory fallback does not persist between ticks, which would mean
    re-posting every parked gate on every tick — so it is for `langgraph dev`
    and nothing else. Deployed, ``get_store()`` returns the managed Postgres
    store.
    """
    try:
        from langgraph.config import get_store

        store = get_store()
        if store is not None:
            return store
    except Exception:  # noqa: BLE001 - not running inside a graph runtime
        pass
    from pagerduty_triage.ledger import InMemoryStore

    return InMemoryStore()


def notify_node(state: NotifierState) -> NotifierState:
    from pagerduty_triage.slack.client import HttpSlackClient
    from pagerduty_triage.slack.platform import PlatformThreads

    settings = load_slack_settings()
    missing = settings.missing()
    if missing:
        # Loud, not silent. A notifier that cannot reach Slack means gates
        # park with nobody told, which looks exactly like "no tickets today".
        return {
            "report": f"not configured; unset: {', '.join(missing)}",
            "posted": [],
            "errors": [f"missing env: {', '.join(missing)}"],
        }

    report = notify_parked_gates(
        source=PlatformThreads(
            url=settings.langgraph_api_url, api_key=settings.langgraph_api_key
        ),
        slack=HttpSlackClient(settings.bot_token),
        store=_store(),
        settings=settings,
        limit=int(os.environ.get("SLACK_NOTIFY_LIMIT", "50")),
    )
    return {
        "report": report.summary(),
        "posted": report.posted,
        "errors": report.errors,
    }


def make_notifier_graph():
    builder = StateGraph(NotifierState)
    builder.add_node("notify", notify_node)
    builder.add_edge(START, "notify")
    builder.add_edge("notify", END)
    return observe(builder.compile())
