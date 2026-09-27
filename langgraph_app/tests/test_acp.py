"""Discovery by the Agent Production Control Plane (``pagerduty_triage.acp``).

The SDK is replaced by a recording fake in ``sys.modules``, so nothing here
touches a network. What these tests pin down:

* telemetry failures never raise into the application, and never log the
  exception text or the credential;
* registration happens at most once per process and heartbeats once after;
* each root graph run becomes exactly one control-plane run, identified only
  by the platform's run UUID -- a gate's ``interrupt()`` is a normal end, and
  an application exception still propagates.
"""

from __future__ import annotations

import logging
import sys
import types
from dataclasses import dataclass
from typing import Any, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from starlette.testclient import TestClient

from pagerduty_triage import acp

SECRET = "acp-credential-must-never-be-logged"


@dataclass
class _Registration:
    workload_id: str = "wl-0001"


@dataclass
class _Run:
    external_run_id: str


class FakeSDK(types.ModuleType):
    def __init__(self) -> None:
        super().__init__("acp_sdk")
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.register_error: BaseException | None = None
        self.bad_digest = False

    def register_agent(self, **kwargs: Any) -> _Registration:
        self.calls.append(("register_agent", kwargs))
        if self.register_error is not None:
            raise self.register_error
        return _Registration()

    def heartbeat(self, **kwargs: Any) -> None:
        self.calls.append(("heartbeat", kwargs))
        if self.bad_digest and kwargs.get("release_digest"):
            raise ValueError("release_digest must be ...")

    def start_run(self, **kwargs: Any) -> _Run:
        self.calls.append(("start_run", kwargs))
        return _Run(kwargs["external_run_id"])

    def finish_run(self, run: _Run, **kwargs: Any) -> None:
        self.calls.append(("finish_run", {"run": run.external_run_id, **kwargs}))

    def flush(self, **kwargs: Any) -> bool:
        self.calls.append(("flush", kwargs))
        return True

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> FakeSDK:
    fake = FakeSDK()
    monkeypatch.setitem(sys.modules, "acp_sdk", fake)
    monkeypatch.setenv("ACP_ENDPOINT", "https://acp.example.test")
    monkeypatch.setenv("ACP_CREDENTIAL", SECRET)
    monkeypatch.setenv("ACP_INSTANCE_KEY", "pod-abc-123")
    monkeypatch.delenv("ACP_RELEASE_DIGEST", raising=False)
    monkeypatch.delenv("LANGCHAIN_REVISION_ID", raising=False)
    monkeypatch.setattr(acp, "_started", False)
    monkeypatch.setattr(acp, "_registered", acp.threading.Event())
    return fake


# -- registration ------------------------------------------------------------


def test_register_sends_fixed_identity_then_heartbeats(sdk, monkeypatch, caplog):
    monkeypatch.setenv("ACP_RELEASE_DIGEST", "sha256:abc")
    caplog.set_level(logging.INFO, logger=acp.__name__)

    assert acp.register() is True

    assert sdk.calls[0] == (
        "register_agent",
        {
            "agent_key": "pagerduty-triage",
            "name": acp.AGENT_NAME,
            "framework": "langgraph",
            "instance_key": "pod-abc-123",
        },
    )
    assert sdk.calls[1] == ("heartbeat", {"release_digest": "sha256:abc"})
    assert acp.is_registered()
    assert "workload_id=wl-0001" in caplog.text
    assert "instance_key=pod-abc-123" in caplog.text
    assert SECRET not in caplog.text


def test_no_tenant_or_environment_is_ever_passed(sdk):
    acp.register()
    kwargs = sdk.calls[0][1]
    assert not {"tenant", "tenant_id", "environment", "environment_id"} & set(kwargs)


@pytest.mark.parametrize(
    "error",
    [
        PermissionError("registration refused (403, code x): " + SECRET),
        ConnectionError("unreachable " + SECRET),
        ValueError("registration rejected (422) " + SECRET),
        RuntimeError("an SDK bug " + SECRET),
    ],
)
def test_registration_failure_is_swallowed_with_a_fixed_message(sdk, caplog, error):
    sdk.register_error = error

    assert acp.register() is False

    assert not acp.is_registered()
    assert "heartbeat" not in sdk.names()
    assert type(error).__name__ in caplog.text
    # Only the type, never the message: SDK text can carry transport detail.
    assert SECRET not in caplog.text
    assert str(error) not in caplog.text


def test_unconfigured_process_runs_unobserved_without_calling_the_sdk(sdk, monkeypatch):
    monkeypatch.delenv("ACP_CREDENTIAL")
    assert acp.register() is False
    assert sdk.calls == []


def test_rejected_release_digest_still_heartbeats(sdk, monkeypatch):
    monkeypatch.setenv("ACP_RELEASE_DIGEST", "not a valid digest")
    sdk.bad_digest = True

    assert acp.register() is True
    assert sdk.calls[1:] == [
        ("heartbeat", {"release_digest": "not a valid digest"}),
        ("heartbeat", {}),
    ]


def test_release_digest_falls_back_to_the_platform_revision(sdk, monkeypatch):
    monkeypatch.setenv("LANGCHAIN_REVISION_ID", "0123abcd")
    assert acp.release_digest() == "0123abcd"
    monkeypatch.setenv("ACP_RELEASE_DIGEST", "sha256:img")
    assert acp.release_digest() == "sha256:img"


def test_start_registers_at_most_once_per_process(sdk):
    for _ in range(3):
        acp.start()
    assert acp._registered.wait(5)
    assert sdk.names().count("register_agent") == 1


def test_instance_key_is_printable_ascii_without_spaces(monkeypatch):
    monkeypatch.setenv("ACP_INSTANCE_KEY", " pod name\twith spaces-é ")
    assert acp.instance_key() == "podnamewithspaces-"
    monkeypatch.setenv("ACP_INSTANCE_KEY", "x" * 300)
    assert len(acp.instance_key()) == 256
    monkeypatch.delenv("ACP_INSTANCE_KEY")
    assert acp.instance_key()


# -- the lifespan is the process startup/shutdown hook -----------------------


def test_http_app_lifespan_registers_and_flushes(sdk):
    from pagerduty_triage.slack import http_app

    with TestClient(http_app.app):
        assert acp._registered.wait(5)
    assert sdk.names()[-1] == "flush"
    assert sdk.calls[-1] == ("flush", {"timeout_s": 5.0})


def test_flush_is_skipped_when_never_registered(sdk):
    acp.flush()
    assert "flush" not in sdk.names()


# -- runs --------------------------------------------------------------------


class _S(TypedDict):
    x: str


def _graph(node) -> Any:
    b = StateGraph(_S)
    b.add_node("n", node)
    b.add_edge(START, "n")
    b.add_edge("n", END)
    return b


def _runs(sdk: FakeSDK) -> list[tuple[str, dict[str, Any]]]:
    return [c for c in sdk.calls if c[0] in {"start_run", "finish_run"}]


RUN_ID = "11111111-1111-1111-1111-111111111111"


def test_a_root_run_is_one_control_plane_run_keyed_by_the_platform_run_id(sdk):
    acp.register()
    graph = acp.observe(_graph(lambda s: {"x": "customer text"}).compile())

    graph.invoke({"x": "customer question"}, {"run_id": RUN_ID})

    assert _runs(sdk) == [
        ("start_run", {"external_run_id": RUN_ID}),
        ("finish_run", {"run": RUN_ID, "status": "succeeded"}),
    ]
    assert "customer" not in repr(sdk.calls)


def test_parking_on_a_gate_is_a_normal_end_and_the_resume_is_a_new_run(sdk):
    acp.register()
    graph = acp.observe(
        _graph(lambda s: {"x": interrupt("approve?")}).compile(
            checkpointer=InMemorySaver()
        )
    )
    thread = {"configurable": {"thread_id": "t1"}}

    graph.invoke({"x": ""}, {**thread, "run_id": RUN_ID})
    graph.invoke(Command(resume="yes"), thread)

    runs = _runs(sdk)
    assert [(n, kw.get("status")) for n, kw in runs] == [
        ("start_run", None),
        ("finish_run", "succeeded"),
        ("start_run", None),
        ("finish_run", "succeeded"),
    ]
    assert runs[0][1]["external_run_id"] == RUN_ID
    assert runs[2][1]["external_run_id"] != RUN_ID


def test_a_failed_run_is_reported_failed_and_the_exception_still_propagates(sdk):
    acp.register()

    def boom(state):
        raise KeyError("secret customer detail")

    graph = acp.observe(_graph(boom).compile())

    with pytest.raises(KeyError):
        graph.invoke({"x": ""}, {"run_id": RUN_ID})

    assert _runs(sdk)[-1] == ("finish_run", {"run": RUN_ID, "status": "failed"})
    assert "secret customer detail" not in repr(sdk.calls)


def test_runs_before_registration_are_not_reported(sdk):
    graph = acp.observe(_graph(lambda s: {"x": "y"}).compile())
    graph.invoke({"x": ""})
    assert _runs(sdk) == []


def test_an_sdk_failure_never_breaks_the_run(sdk, monkeypatch):
    acp.register()

    def broken(**kwargs):
        raise RuntimeError("sdk down")

    monkeypatch.setattr(sdk, "start_run", broken)
    graph = acp.observe(_graph(lambda s: {"x": "done"}).compile())

    assert graph.invoke({"x": ""})["x"] == "done"


def test_every_deployed_graph_is_observed():
    from pagerduty_triage.poller_graph import make_poller_graph
    from pagerduty_triage.slack.notifier_graph import make_notifier_graph

    for graph in (make_poller_graph(), make_notifier_graph()):
        assert acp._observer in graph.config["callbacks"]
