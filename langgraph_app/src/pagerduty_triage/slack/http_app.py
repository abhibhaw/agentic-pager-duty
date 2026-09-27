"""The HTTP surface Slack POSTs to. Twenty lines of plumbing, no decisions.

## Where this runs: on the LangGraph deployment itself

LangGraph Platform hosts custom routes. Point ``langgraph.json`` at this
module and the route is served on the same origin as ``/threads`` and
``/runs``:

```json
"http": { "app": "./src/pagerduty_triage/slack/http_app.py:app" }
```

Custom routes are **merged, not sub-mounted**, and they take priority over
the platform's own — so the path is namespaced under ``/slack/`` where it
cannot shadow a system endpoint.

No separate service is needed. This module is nonetheless a plain Starlette
app with no LangGraph imports, so ``uvicorn
pagerduty_triage.slack.http_app:app`` runs it standalone if the endpoint ever
has to be isolated from the deployment that holds the Zoho and GitHub
credentials. That is a config change, not a rewrite.

## The route is public, deliberately, and that is the whole reason for signing

``http.enable_custom_route_auth`` defaults to ``false``: custom routes bypass
the API-key auth protecting ``/threads`` and ``/runs``. We leave it false,
because Slack cannot present a LangGraph API key. The flag is also
all-or-nothing — turning it on would break this route and every other custom
one — so per-route auth would mean branching on ``path`` inside
``@auth.authenticate``, which is more machinery for a check that would still
not tell us *which human* clicked.

So the endpoint is internet-reachable and ``signing.py`` is the entire
boundary. See its module docstring.

## Three seconds

Slack expects an HTTP 200 within three seconds or it shows the reviewer an
error. Verification and authorization are pure CPU and happen inline; the
resume and the message update are handed to a Starlette ``BackgroundTask``,
which runs *after* the response has been sent.
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Any

from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from pagerduty_triage import acp
from pagerduty_triage.slack.config import SlackSettings, load_slack_settings
from pagerduty_triage.slack.handler import handle_interaction

#: Namespaced so a custom route cannot shadow a platform endpoint.
INTERACTIONS_PATH = "/slack/interactions"
HEALTH_PATH = "/slack/health"
#: The platform calls this when a run ends -- including when it ends by parking
#: on an `interrupt()`, which is the case we care about. See `run_finished`.
RUN_FINISHED_PATH = "/slack/run-finished"

logger = logging.getLogger(__name__)


def _deps(settings: SlackSettings) -> tuple[Any, Any]:
    """Real clients, built per request so a redeployed secret is picked up.

    Imported lazily: importing this module must not require ``langgraph_sdk``
    or a reachable platform, because the tests import it.
    """
    from pagerduty_triage.slack.client import HttpSlackClient
    from pagerduty_triage.slack.platform import PlatformThreads

    return (
        HttpSlackClient(settings.bot_token),
        PlatformThreads(
            url=settings.langgraph_api_url, api_key=settings.langgraph_api_key
        ),
    )


async def interactions(request: Request) -> Response:
    settings = load_slack_settings()

    # `await request.body()` before anything else parses it. The HMAC covers
    # these exact bytes; a re-encoded form body produces a different string
    # and a signature that never matches.
    raw_body = await request.body()

    slack, threads = _deps(settings)
    result = handle_interaction(
        raw_body=raw_body,
        headers=dict(request.headers),
        settings=settings,
        slack=slack,
        threads=threads,
    )

    background = BackgroundTask(result.followup) if result.followup else None
    # An empty 200 is Slack's documented bare acknowledgement: the reviewer
    # sees nothing change until the background task talks to `response_url`.
    return PlainTextResponse(
        result.body, status_code=result.status, background=background
    )


async def health(request: Request) -> Response:
    """Is this deployment able to answer a gate at all?

    Reports only whether each variable is *set*, never its value, so the
    endpoint is safe to leave public alongside the one that has to be.
    """
    settings = load_slack_settings()

    # Reported, not required. A deployment with no webhook config works — gates
    # park correctly, they are simply never announced, and somebody has to
    # notice unaided. That exact configuration shipped once and looked perfectly
    # healthy: ok true, nothing missing, and a ticket sitting at a gate nobody
    # had been told about. Silence is the failure mode this line exists to break.
    webhook_unset = [
        name
        for name in ("PAGER_PUBLIC_URL", "PAGER_WEBHOOK_SECRET")
        if not os.environ.get(name, "").strip()
    ]

    return JSONResponse(
        {
            "ok": not settings.missing(),
            "missing_env": settings.missing(),
            "approvers_configured": len(settings.approver_user_ids),
            # "manual" means someone must run the notifier by hand after a gate
            # parks. Half-configured counts as manual: either half alone
            # announces nothing.
            "gate_notification": "automatic" if not webhook_unset else "manual",
            "webhook_unset": webhook_unset,
        }
    )


def _run_notifier_sweep() -> None:
    """Post any parked gate that has not been posted yet.

    Starts a run of the ``slack_notifier`` assistant rather than calling
    ``notify_parked_gates`` inline. That is not indirection for its own sake:
    the notifier dedupes against the **platform store**, and ``get_store()``
    only returns the managed store inside a graph. Called inline from this HTTP
    route it would fall back to an in-memory store, which does not persist --
    so every callback would re-post every parked gate.

    No ``webhook`` is passed here, so the notifier run finishing does not call
    this endpoint again. Only triage runs carry the callback.

    Imported lazily and kept behind a module-level name so the tests can
    replace it without a platform, a Slack token or a network.
    """
    from langgraph_sdk import get_sync_client

    settings = load_slack_settings()
    client = get_sync_client(
        url=settings.langgraph_api_url or None, api_key=settings.langgraph_api_key or None
    )
    client.runs.create(None, "slack_notifier", input={})


async def run_finished(request: Request) -> Response:
    """Called by LangGraph Platform when a run ends.

    This is what replaces the notifier cron. A run that parks on `interrupt()`
    *ends* -- so the callback fires, and the gate card lands seconds later
    rather than on the next tick.

    **The boundary is a shared secret in the query string.** Unlike Slack, the
    platform does not sign its callbacks, and `enable_custom_route_auth` is
    false so this route bypasses the API key (see the module docstring). An
    unset secret closes the endpoint rather than opening it, for the same
    reason an unset Slack signing secret does.

    The payload is deliberately ignored. The sweep re-reads thread state and
    posts what is actually parked, which makes the callback a *hint* rather than
    a source of truth -- so a duplicate, a retry, or a callback for an unrelated
    run all collapse to the same harmless re-check.
    """
    expected = os.environ.get("PAGER_WEBHOOK_SECRET", "")
    supplied = request.query_params.get("token", "")
    if not expected or not hmac.compare_digest(expected, supplied):
        return PlainTextResponse("forbidden", status_code=403)

    # NOT inline. `_run_notifier_sweep` uses the synchronous SDK client and the
    # request it makes comes back to *this* server -- called from this async
    # route it blocks the event loop, so the server cannot answer its own call
    # and the webhook times out. Observed live as HTTP 000 after 30s, with the
    # gate never announced. Starlette runs a sync BackgroundTask in a
    # threadpool, which frees the loop and answers the platform immediately.
    #
    # The platform retries a webhook that does not return 2xx, so a failed sweep
    # is logged and still acknowledged: a Slack outage must not become a retry
    # storm, and `notify_parked_gates` is idempotent anyway.
    def _sweep() -> None:
        try:
            _run_notifier_sweep()
        except Exception:  # noqa: BLE001
            logger.exception("notifier sweep failed for a run-finished callback")

    return PlainTextResponse("ok", background=BackgroundTask(_sweep))


# The lifespan is how this deployment registers with the Agent Production
# Control Plane: LangGraph Platform enters a custom app's lifespan once in every
# server and queue-worker process. Registration runs on a background thread, so
# startup is not delayed and a control-plane outage cannot fail readiness. See
# acp.py.
app = Starlette(
    lifespan=acp.lifespan,
    routes=[
        Route(INTERACTIONS_PATH, interactions, methods=["POST"]),
        Route(HEALTH_PATH, health, methods=["GET"]),
        Route(RUN_FINISHED_PATH, run_finished, methods=["POST"]),
    ]
)
