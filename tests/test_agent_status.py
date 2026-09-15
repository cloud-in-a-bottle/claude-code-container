from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from litestar import Litestar
from litestar.testing import TestClient

from server import agent_status
from server import app as srv
from server.agent_status import AgentReport
from server.projects import store
from server.projects.workspaces import Workspace
from server.tabs import ServerTab
from server.tabs import _tabs

CLAUDE_HOME = Path(__file__).resolve().parent.parent / "claude-home"
HOOK_SCRIPT = CLAUDE_HOME / "agent_status_hook.py"
INSTALLER = CLAUDE_HOME / "install_agent_hooks.py"

SESSION = "c8eaae3d-5163-4115-944a-60045e5a9f15"
OTHER_SESSION = "9f0f1ac2-1111-2222-3333-444455556666"


@pytest.fixture(autouse=True)
def clear_tabs() -> None:
    _tabs.clear()


def run_hook(home: Path, state: str, event: dict[str, object]) -> subprocess.CompletedProcess[str]:
    """Run the real hook the way Claude Code runs it: JSON on stdin, state in argv."""
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT), state],
        input=json.dumps(event),
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )


def hook_event(session_id: str = SESSION, cwd: str = "/tmp", **extra: object) -> dict[str, object]:
    """The shape Claude Code actually sends, as captured from a live session."""
    return {
        "session_id": session_id,
        "transcript_path": f"/somewhere/{session_id}.jsonl",
        "cwd": cwd,
        "permission_mode": "bypassPermissions",
        "hook_event_name": "Stop",
        **extra,
    }


def written(home: Path, session_id: str = SESSION) -> dict[str, Any]:
    payload = json.loads((home / ".workbench" / "agent-status" / f"{session_id}.json").read_text())
    assert isinstance(payload, dict)
    return payload


def report(state: str, cwd: str, at: float, session_id: str = SESSION, message: str = "") -> AgentReport:
    return AgentReport(session_id=session_id, state=state, cwd=cwd, at=at, message=message)


def write_report(entry: AgentReport) -> None:
    agent_status.AGENT_STATUS_DIR.mkdir(parents=True, exist_ok=True)
    path = agent_status.AGENT_STATUS_DIR / f"{entry.session_id}.json"
    path.write_text(
        json.dumps(
            {
                "session_id": entry.session_id,
                "state": entry.state,
                "cwd": entry.cwd,
                "at": entry.at,
                "message": entry.message,
            }
        )
    )


# ── the hook the agent runs ────────────────────────────────────────────────────


def test_the_hook_records_the_state_it_was_given(tmp_path: Path) -> None:
    run_hook(tmp_path, "working", hook_event(cwd="/workspaces/p/w"))

    payload = written(tmp_path)
    assert payload["state"] == "working"
    assert payload["cwd"] == "/workspaces/p/w"
    assert payload["session_id"] == SESSION
    assert float(payload["at"]) == pytest.approx(time.time(), abs=30)


def test_the_hook_records_where_the_transcript_is(tmp_path: Path) -> None:
    """It's the only sign of life a turn gives off between starting and finishing."""
    run_hook(tmp_path, "working", hook_event())
    assert written(tmp_path)["transcript"] == f"/somewhere/{SESSION}.jsonl"


def test_the_hook_keeps_the_last_thing_the_agent_said(tmp_path: Path) -> None:
    """`Stop` carries it, and the card shows it — an idle agent's last word is the useful part."""
    run_hook(tmp_path, "idle", hook_event(last_assistant_message="done"))
    assert written(tmp_path)["message"] == "done"


def test_the_hook_truncates_a_long_message(tmp_path: Path) -> None:
    run_hook(tmp_path, "idle", hook_event(last_assistant_message="x" * 5000))
    assert len(str(written(tmp_path)["message"])) == 200


def test_the_end_of_a_session_removes_its_report(tmp_path: Path) -> None:
    run_hook(tmp_path, "working", hook_event())
    path = tmp_path / ".workbench" / "agent-status" / f"{SESSION}.json"
    assert path.exists()

    run_hook(tmp_path, "gone", hook_event())
    assert not path.exists()


@pytest.mark.parametrize("session_id", ["../../escape", "", "with/slash", "x" * 200])
def test_the_hook_refuses_a_session_id_that_is_not_one(tmp_path: Path, session_id: str) -> None:
    """The id becomes a filename, and this runs as root in the user's home."""
    run_hook(tmp_path, "working", hook_event(session_id=session_id))
    assert (
        list((tmp_path / ".workbench" / "agent-status").glob("*")) == []
        or not (tmp_path / ".workbench" / "agent-status").exists()
    )


@pytest.mark.parametrize("stdin", ["", "not json", "[]", "null"])
def test_the_hook_survives_junk_on_stdin(tmp_path: Path, stdin: str) -> None:
    """A hook that raises prints into the conversation, so it never raises."""
    done = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT), "working"],
        input=stdin,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0
    assert done.stdout == "" and done.stderr == ""


# ── installing it ──────────────────────────────────────────────────────────────


def _install(home: Path) -> dict[str, Any]:
    subprocess.run([sys.executable, str(INSTALLER)], env={"HOME": str(home)}, check=True, capture_output=True)
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert isinstance(settings, dict)
    return settings


def test_installing_hooks_into_a_home_that_has_none(tmp_path: Path) -> None:
    settings = _install(tmp_path)
    assert set(settings["hooks"]) == {"SessionStart", "UserPromptSubmit", "Notification", "Stop", "SessionEnd"}
    command = settings["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert command.endswith(f"{HOOK_SCRIPT.name} working")
    assert Path(command.split()[0]).is_absolute()  # never depends on the agent's PATH


def test_installing_hooks_keeps_the_rest_of_the_settings(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"theme": "light", "hooks": {"PreCompact": [{"hooks": ["mine"]}]}}))

    settings = _install(tmp_path)
    assert settings["theme"] == "light"
    assert settings["hooks"]["PreCompact"] == [{"hooks": ["mine"]}]


def test_installing_hooks_over_a_broken_settings_file(tmp_path: Path) -> None:
    """Better to write a usable file than to leave Claude Code with an unreadable one."""
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ not json")

    assert "Stop" in _install(tmp_path)["hooks"]


def test_installing_twice_changes_nothing(tmp_path: Path) -> None:
    """It runs on every container start."""
    assert _install(tmp_path) == _install(tmp_path)


# ── reading the reports back ───────────────────────────────────────────────────


def test_reading_reports_newest_first(workbench_home: Path) -> None:
    write_report(report("idle", "/a", at=100.0))
    write_report(report("working", "/b", at=200.0, session_id=OTHER_SESSION))

    assert [r.at for r in agent_status.read_reports()] == [200.0, 100.0]


def test_unreadable_reports_are_skipped(workbench_home: Path) -> None:
    """These are written by a hook in someone else's process; a half-written one can happen."""
    agent_status.AGENT_STATUS_DIR.mkdir(parents=True)
    (agent_status.AGENT_STATUS_DIR / "broken.json").write_text('{"state": "wor')
    (agent_status.AGENT_STATUS_DIR / "nonsense.json").write_text('{"state": "elated"}')
    write_report(report("working", "/a", at=100.0))

    assert [r.session_id for r in agent_status.read_reports()] == [SESSION]


def test_a_report_from_a_live_session_never_goes_stale(workbench_home: Path) -> None:
    """An agent left thinking overnight is still thinking, and its tab proves it's still there."""
    old = report("working", "/a", at=time.time() - 10 * agent_status.STALE_AFTER_SECONDS)
    assert agent_status.is_current(old, frozenset({SESSION}), time.time())


def test_a_report_from_a_dead_session_expires(workbench_home: Path) -> None:
    """A killed agent never gets to send SessionEnd, so its last state must not outlive it."""
    old = report("working", "/a", at=time.time() - agent_status.STALE_AFTER_SECONDS - 1)
    assert not agent_status.is_current(old, frozenset(), time.time())


def test_prune_only_takes_the_old_ones(workbench_home: Path) -> None:
    write_report(report("idle", "/a", at=100.0))
    write_report(report("working", "/b", at=200.0, session_id=OTHER_SESSION))
    os.utime(agent_status.AGENT_STATUS_DIR / f"{SESSION}.json", (0, 0))

    agent_status.prune_reports(time.time() - 60)
    assert [p.stem for p in agent_status.AGENT_STATUS_DIR.glob("*.json")] == [OTHER_SESSION]


# ── placing them in workspaces ─────────────────────────────────────────────────


def test_an_agent_in_a_subdirectory_still_belongs_to_its_workspace(workbench_home: Path) -> None:
    workspace = Workspace(project_id="proj", name="ws")
    deep = str(workspace.path / "src" / "server")
    assert agent_status.workspace_for(deep, (workspace,)) == workspace


def test_an_agent_outside_every_workspace_belongs_to_none(workbench_home: Path) -> None:
    workspace = Workspace(project_id="proj", name="ws")
    assert agent_status.workspace_for("/tmp/elsewhere", (workspace,)) is None
    assert agent_status.workspace_for("", (workspace,)) is None


def test_a_workspace_takes_the_state_that_most_wants_a_human(workbench_home: Path) -> None:
    workspace = Workspace(project_id="proj", name="ws")
    now = time.time()
    write_report(report("working", str(workspace.path), at=now))
    write_report(report("waiting", str(workspace.path), at=now - 5, session_id=OTHER_SESSION))

    statuses = agent_status.statuses_for((workspace,), frozenset(), now)
    assert len(statuses) == 1
    assert statuses[0].state == "waiting"  # outranks the busy one: only this one is blocked on you
    assert statuses[0].agents == 2


def test_a_workspace_with_no_agent_gets_no_status(workbench_home: Path) -> None:
    workspace = Workspace(project_id="proj", name="ws")
    write_report(report("working", "/somewhere/else", at=time.time()))

    assert agent_status.statuses_for((workspace,), frozenset()) == ()


# ── the route ──────────────────────────────────────────────────────────────────


def _client() -> TestClient[Litestar]:
    return TestClient(app=srv.app)


def test_the_route_reports_per_workspace(workbench_home: Path) -> None:
    store.add_project("proj", "https://example.com/p.git")
    workspace = Workspace(project_id="proj", name="ws")
    workspace.path.mkdir(parents=True)
    write_report(report("working", str(workspace.path), at=time.time(), message="hi"))

    body = _client().get("/api/workspaces/agents").json()
    assert body == [
        {
            "workspace_id": "proj/ws",
            "state": "working",
            "since": pytest.approx(time.time(), abs=30),
            "agents": 1,
            "message": "hi",
        }
    ]


def test_the_route_keeps_a_running_tab_on_the_board(workbench_home: Path) -> None:
    """A session the workbench still has a tab for is never dropped, however quiet it has gone --
    but a *working* report that old has clearly stopped working, and settles to idle."""
    store.add_project("proj", "https://example.com/p.git")
    workspace = Workspace(project_id="proj", name="ws")
    workspace.path.mkdir(parents=True)
    a_day_ago = time.time() - 60 * 60 * 24
    write_report(report("idle", str(workspace.path), at=a_day_ago))
    _tabs["t1"] = ServerTab(
        id="t1",
        label="claude",
        master_fd=-1,
        proc=None,  # type: ignore[arg-type]
        session_id=SESSION,
        workspace_id=workspace.id,
    )

    assert [s["state"] for s in _client().get("/api/workspaces/agents").json()] == ["idle"]

    write_report(report("working", str(workspace.path), at=a_day_ago))
    assert [s["state"] for s in _client().get("/api/workspaces/agents").json()] == ["idle"]


def test_the_route_is_empty_without_reports(workbench_home: Path) -> None:
    store.add_project("proj", "https://example.com/p.git")
    assert _client().get("/api/workspaces/agents").json() == []


# ── a turn that was interrupted ────────────────────────────────────────────────


def working(at: float, transcript: str = "") -> AgentReport:
    return AgentReport(session_id=SESSION, state="working", cwd="/w", at=at, transcript=transcript)


def test_a_turn_that_just_started_is_working(workbench_home: Path) -> None:
    now = time.time()
    assert agent_status.settled(working(now), now).state == "working"


def test_a_working_report_with_nothing_behind_it_settles_to_idle(workbench_home: Path) -> None:
    """Nothing fires a hook when a turn is interrupted, so "working" has to time out on its own.

    Without this the sidebar pulsed at a session that stopped the moment escape was pressed -- and
    since the workbench still had its tab, it pulsed forever.
    """
    now = time.time()
    stale = working(now - agent_status.WORKING_GRACE_SECONDS - 1)
    assert agent_status.settled(stale, now).state == agent_status.IDLE


def test_a_long_tool_call_keeps_the_turn_alive(workbench_home: Path) -> None:
    """Claude Code appends to the transcript at every tool call; that's the heartbeat."""
    now = time.time()
    transcript = workbench_home / "transcript.jsonl"
    transcript.write_text("{}\n")
    os.utime(transcript, (now - 5, now - 5))

    old_report = working(now - agent_status.WORKING_GRACE_SECONDS - 1, str(transcript))
    assert agent_status.settled(old_report, now).state == "working"


def test_a_transcript_as_stale_as_the_report_does_not_save_it(workbench_home: Path) -> None:
    now = time.time()
    transcript = workbench_home / "transcript.jsonl"
    transcript.write_text("{}\n")
    long_ago = now - agent_status.WORKING_GRACE_SECONDS - 10
    os.utime(transcript, (long_ago, long_ago))

    assert agent_status.settled(working(long_ago, str(transcript)), now).state == agent_status.IDLE


def test_a_transcript_that_is_gone_leaves_the_report_on_its_own_age(workbench_home: Path) -> None:
    now = time.time()
    missing = str(workbench_home / "no-such-transcript.jsonl")
    assert agent_status.settled(working(now, missing), now).state == "working"
    assert agent_status.settled(working(now - 10_000, missing), now).state == agent_status.IDLE


@pytest.mark.parametrize("state", ["idle", "waiting"])
def test_the_other_states_are_left_alone(workbench_home: Path, state: str) -> None:
    """Only "working" claims something is happening right now; the rest are resting states."""
    old = AgentReport(session_id=SESSION, state=state, cwd="/w", at=time.time() - 10_000)
    assert agent_status.settled(old, time.time()).state == state


def test_an_interrupted_session_stops_showing_as_working(workbench_home: Path) -> None:
    """End to end: the report a live session left behind no longer lights up its workspace."""
    workspace = Workspace(project_id="proj", name="ws")
    write_report(
        AgentReport(
            session_id=SESSION,
            state="working",
            cwd=str(workspace.path),
            at=time.time() - agent_status.WORKING_GRACE_SECONDS - 60,
        )
    )
    # Its tab is still running, which is exactly what used to make this permanent.
    statuses = agent_status.statuses_for((workspace,), frozenset({SESSION}))
    assert [s.state for s in statuses] == [agent_status.IDLE]
