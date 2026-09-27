"""The graph: one deep agent, three subagents, two gates.

Exported as `graph` for `langgraph.json`.

## What the main agent is allowed to do

Almost nothing, deliberately. It holds:

* `write_todos` — the planning tool.
* the virtual filesystem tools — `ls`, `read_file`, `write_file`, `edit_file`,
  `glob`, `grep`.
* `task` — delegation to the three subagents.
* `zoho_fetch_ticket`, `zoho_send_reply`, `create_sprint_issue`.

It does not read source code, does not talk to Zoho beyond the one fetch, and
does not analyse the ticket itself. Subagents do the reading and leave files
behind; the main agent reads a one-line summary and a filename.

## Two things about deepagents 0.7.14 that the design brief assumed wrongly

1. **`write_todos` is NOT a default.** The brief says the main agent "holds
   the planning tool"; deepagents' default middleware stack in 0.7.14 is
   filesystem + subagents + summarization + Anthropic prompt caching, and
   `TodoListMiddleware` is not in it. It must be passed explicitly, which is
   what `_planning_middleware()` below does. Without it there is no
   `write_todos` tool and the main-agent prompt instructs the model to call a
   tool that does not exist.

2. **The built-in filesystem middleware ships an `execute` tool** that runs
   shell commands. A triage agent that reads attacker-influenced customer text
   has no business holding a shell. See `_filesystem_middleware()`.
"""

from __future__ import annotations

import os
from typing import Any

from deepagents import create_deep_agent

from pagerduty_triage.acp import observe
from pagerduty_triage.prompts import (
    MAIN_AGENT_PROMPT,
    PAGER_SCRIBE_PROMPT,
    RESPONDER_PROMPT,
    TRIAGE_ANALYST_PROMPT,
)
from pagerduty_triage.settings import Settings, load_settings
from pagerduty_triage.tools import ToolDeps, build_tools

#: Filesystem tools the agents actually need. Note the absence of `execute`.
#:
#: deepagents' FilesystemMiddleware exposes `ls, read_file, write_file,
#: edit_file, delete, glob, grep, execute`. `execute` runs shell commands in
#: the deployment container. Nothing in triage needs it, and the agent's whole
#: input is text written by strangers, so it is removed rather than merely
#: discouraged in a prompt.
SAFE_FILESYSTEM_TOOLS = ("ls", "read_file", "write_file", "edit_file", "glob", "grep")


def _planning_middleware() -> list[Any]:
    """`TodoListMiddleware`, which deepagents does not install by default."""
    from langchain.agents.middleware import TodoListMiddleware

    return [TodoListMiddleware()]


def _safe_filesystem_middleware() -> Any | None:
    """A fresh `FilesystemMiddleware` restricted to `SAFE_FILESYSTEM_TOOLS`.

    VERIFIED against deepagents 0.7.14 (was a `TODO(verify on first deploy)`):
    passing this through `middleware=` **replaces** the default filesystem
    middleware rather than installing a second one. `deepagents.graph.
    _apply_custom_middleware` merges the caller's middleware into the default
    stack *by `.name`*, and both instances answer to `"FilesystemMiddleware"`,
    so ours is substituted in place and the default — with its `execute`
    tool — never reaches the compiled graph. The same merge runs for subagent
    stacks, which is why each subagent below carries its own instance.

    Returns a new instance per call: a middleware object is stateful wiring
    and is not shared between the main agent and three subagents.
    """
    try:
        from deepagents.middleware.filesystem import FilesystemMiddleware
    except ImportError:  # pragma: no cover - import path moved
        return None
    return FilesystemMiddleware(tools=list(SAFE_FILESYSTEM_TOOLS))


def _filesystem_middleware() -> list[Any]:
    """`_safe_filesystem_middleware()` as a list, for splicing into `middleware=`."""
    middleware = _safe_filesystem_middleware()
    return [middleware] if middleware is not None else []


def _subagents() -> list[dict[str, Any]]:
    """The three subagents.

    Each gets filesystem tools only. None gets `zoho_send_reply` or
    `create_sprint_issue` — not because the gate would fail if they did (it
    would not; `interrupt()` propagates out of a subagent's tool just as it
    does from the main agent's), but because a subagent has no business
    holding a tool that emails a customer. Least privilege, not gate repair.

    `description` is what the main agent sees when choosing whom to delegate
    to, so it is written for that reader.

    VERIFIED against deepagents 0.7.14 (was a `TODO(verify on first deploy)`):
    a subagent's `tools` list does **not** accept tool-name strings. deepagents
    hands `spec["tools"]` straight to `create_agent`, which hands it to
    LangGraph's `ToolNode`; anything that is not a `BaseTool` goes through
    `create_tool()`, and `create_tool("ls")` is the *decorator* form — it
    returns a function, not a tool, and `ToolNode` then dies on
    `'function' object has no attribute 'name'`. So `tools` here is the list of
    *extra* tools, and it must be empty:

    * empty, not absent. An absent `tools` key makes the subagent inherit the
      main agent's tools — including `zoho_send_reply` and
      `create_sprint_issue`. `[]` is what enforces least privilege.
    * the filesystem tools arrive via middleware instead. deepagents gives every
      declarative subagent its own default `FilesystemMiddleware`; passing one
      in `middleware` replaces it by name, which is how the `execute` shell is
      kept off the subagents too.
    """
    return [
        {
            "name": "triage-analyst",
            "description": (
                "Reads /ticket.md and classifies the ticket as platform_query, "
                "k8s_issue, platform_bug, or needs_more_info. Writes /triage.md "
                "with the classification, its confidence, and the evidence. "
                "Delegate this first, always."
            ),
            "system_prompt": TRIAGE_ANALYST_PROMPT,
            "tools": [],
            "middleware": _filesystem_middleware(),
        },
        {
            "name": "responder",
            "description": (
                "Drafts the customer-facing reply for a platform_query, a "
                "k8s_issue, or a needs_more_info clarification. Reads "
                "/ticket.md and /triage.md, writes /draft_reply.md containing "
                "the reply body and nothing else."
            ),
            "system_prompt": RESPONDER_PROMPT,
            "tools": [],
            "middleware": _filesystem_middleware(),
        },
        {
            "name": "pager-scribe",
            "description": (
                "Fills the sprint-tasks pager template for a confirmed "
                "platform_bug. Reads /ticket.md and /triage.md, writes "
                "/pager_issue.md containing a single ```json block of template "
                "fields. Knows the controlled vocabularies."
            ),
            "system_prompt": PAGER_SCRIBE_PROMPT,
            "tools": [],
            "middleware": _filesystem_middleware(),
        },
    ]


#: Name deepagents gives the subagent it adds on its own.
GENERAL_PURPOSE = "general-purpose"


def _general_purpose_override() -> dict[str, Any]:
    """Our own `general-purpose` spec, replacing the one deepagents auto-adds.

    Found the first time the graph actually compiled: when no spec named
    `general-purpose` is supplied, `create_deep_agent` inserts one *that
    inherits the main agent's tools*. On this graph that handed a subagent
    `zoho_send_reply` and `create_sprint_issue` — the two tools the module
    docstring says no subagent holds.

    It is not a gate bypass (`interrupt()` fires from a subagent's tool just as
    it does from the main agent's), but it is exactly the least-privilege
    deviation `_subagents()` exists to prevent, and it was invisible while the
    graph could not be built. deepagents documents supplying an explicit spec
    as the way to override the default, and that is what this is.

    `_subagents()` deliberately does not include this: it describes the three
    subagents this system designed, and the tests that count them should keep
    counting three.
    """
    return {
        "name": GENERAL_PURPOSE,
        "description": (
            "Fallback worker with filesystem access and nothing else. Prefer "
            "triage-analyst, responder or pager-scribe — each knows this "
            "system's vocabulary and this one does not."
        ),
        "system_prompt": (
            "You are a general-purpose worker on a Zoho Desk triage agent. You "
            "have the virtual filesystem and nothing else. Do the task you were "
            "given, write your output to a file, and report the filename."
        ),
        "tools": [],
        "middleware": _filesystem_middleware(),
    }


def build_agent(deps: ToolDeps, *, checkpointer: Any | None = None):
    """Build the compiled deep agent.

    ``checkpointer`` must be **None on LangGraph Platform** — the managed
    runtime provisions Postgres persistence itself, and a graph compiled with
    its own checkpointer will not use it. Pass one only for local runs
    (`local_dev.py` does). The interrupts depend on persistence, so this is
    not an optional detail: without a checkpointer a gate cannot park.
    """
    return create_deep_agent(
        model=resolve_model(deps.settings),
        tools=build_tools(deps),
        system_prompt=MAIN_AGENT_PROMPT,
        subagents=[*_subagents(), _general_purpose_override()],
        middleware=[*_planning_middleware(), *_filesystem_middleware()],
        checkpointer=checkpointer,
    )


def resolve_model(settings: Settings) -> Any:
    """The model to hand ``create_deep_agent``: a string, or a built model.

    Without a gateway this returns ``settings.model`` unchanged and
    ``init_chat_model`` resolves it, which is the behaviour this project has
    always had.

    With ``LLM_GATEWAY_BASE_URL`` set it must return a *built* model, because a
    provider-prefixed string cannot carry a base URL. That distinction is the
    whole reason this function exists: passing the string would call the
    provider directly and **succeed**, silently bypassing the gateway -- no
    governance, no gateway-side tracing, and a provider key still in use. A
    failure that looks like success is the one worth a test.

    The LangSmith gateway is OpenAI-compatible (`POST /v1/chat/completions`)
    and authenticates with a LangSmith key, so models are addressed by prefixed
    id (`anthropic/claude-opus-5`) through the OpenAI client.
    """
    base_url = settings.llm_gateway_base_url.strip()
    if not base_url:
        return settings.model

    key = settings.llm_gateway_api_key.strip()
    if not key:
        # Falling through here would let ChatOpenAI pick up OPENAI_API_KEY from
        # the environment and talk to OpenAI instead of the gateway.
        raise RuntimeError(
            "LLM_GATEWAY_BASE_URL is set but no gateway key is: set "
            "LANGSMITH_API_KEY (or LLM_GATEWAY_API_KEY). Refusing to start "
            "rather than silently bypass the gateway."
        )

    from langchain.chat_models import init_chat_model

    return init_chat_model(
        settings.model,
        model_provider="openai",
        base_url=base_url,
        api_key=key,
    )


def build_deps(settings: Settings | None = None) -> ToolDeps:
    """Wire the clients named by the environment.

    Deliberately fails loudly on a half-configured deployment rather than
    quietly falling back to the fake and pretending to work.
    """
    from pagerduty_triage.ledger import InMemoryStore, TicketLedger
    from pagerduty_triage.sprint_tasks import FakeSprintTasksClient, GitHubSprintTasksClient
    from pagerduty_triage.zoho.fake import FakeZohoDeskClient

    settings = settings or load_settings()

    missing = settings.missing_for_transport()
    if missing:
        raise RuntimeError(
            f"ZOHO_TRANSPORT={settings.zoho_transport!r} needs these environment "
            f"variables, which are unset: {', '.join(missing)}"
        )

    if settings.zoho_transport == "rest":
        from pagerduty_triage.zoho.rest import RestZohoDeskClient

        zoho: Any = RestZohoDeskClient(settings)
    elif settings.zoho_transport == "mcp":
        from pagerduty_triage.zoho.mcp import McpZohoDeskClient

        zoho = McpZohoDeskClient(settings)
    else:
        zoho = FakeZohoDeskClient()

    if settings.github_token:
        sprint: Any = GitHubSprintTasksClient(
            settings.github_token, settings.sprint_tasks_repo
        )
    else:
        sprint = FakeSprintTasksClient(settings.sprint_tasks_repo)

    # On Platform the store is injected into the graph at runtime; this
    # in-memory instance is the local fallback. See README "Store".
    return ToolDeps(
        settings=settings,
        zoho=zoho,
        sprint_tasks=sprint,
        ledger=TicketLedger(InMemoryStore()),
    )


def make_graph():
    """Factory referenced by `langgraph.json`.

    A factory rather than a module-level singleton so that import of this
    module — which happens in tests — does not require a configured
    environment.

    `observe` reports each platform run to the Agent Production Control Plane
    (see acp.py). It sends the run's UUID and outcome, never its content.
    """
    return observe(build_agent(build_deps()))


# `langgraph.json` may point at either `agent.py:graph` or `agent.py:make_graph`.
# We export the factory and only build eagerly when explicitly asked, so that
# `import pagerduty_triage.agent` stays cheap and side-effect free.
if os.environ.get("PAGERDUTY_EAGER_GRAPH") == "1":  # pragma: no cover
    graph = make_graph()
