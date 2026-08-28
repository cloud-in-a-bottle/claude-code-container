from __future__ import annotations

import os
from pathlib import Path

import pytest

from server import claude_sessions
from server.claude_sessions import latest_session_id
from server.claude_sessions import transcript_dir

ONE = "11111111-1111-1111-1111-111111111111"
TWO = "22222222-2222-2222-2222-222222222222"


def _write_transcript(cwd: Path, session_id: str, mtime: float) -> Path:
    path = transcript_dir(cwd) / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n")
    os.utime(path, (mtime, mtime))
    return path


@pytest.mark.parametrize(
    ("cwd", "expected"),
    [
        ("/tmp/trusttest", "-tmp-trusttest"),
        # Every character outside [A-Za-z0-9] collapses to a dash, so `_` and `.` are not special.
        ("/data/app_data/ws/claude.md", "-data-app-data-ws-claude-md"),
        ("/home/w/workspaces/openhost/workspace", "-home-w-workspaces-openhost-workspace"),
    ],
)
def test_transcript_dir_matches_the_layout_claude_actually_writes(
    workbench_home: Path, cwd: str, expected: str
) -> None:
    """Checked against the real ~/.claude/projects of a running workbench.

    This is the one assumption in the module that a Claude release could invalidate; if it does,
    every lookup returns "" and the workbench falls back to opening fresh conversations.
    """
    assert transcript_dir(cwd) == claude_sessions.CLAUDE_PROJECTS_DIR / expected


def test_no_session_when_the_directory_has_never_been_used(workbench_home: Path) -> None:
    assert latest_session_id(workbench_home / "never-opened") == ""


def test_finds_the_most_recently_used_conversation(workbench_home: Path) -> None:
    cwd = workbench_home / "ws"
    _write_transcript(cwd, ONE, mtime=1000)
    _write_transcript(cwd, TWO, mtime=2000)
    assert latest_session_id(cwd) == TWO


def test_conversations_in_other_directories_are_not_offered(workbench_home: Path) -> None:
    """Two workspaces are separate conversations even when one has been idle far longer."""
    _write_transcript(workbench_home / "other", ONE, mtime=9000)
    assert latest_session_id(workbench_home / "ws") == ""


def test_ignores_transcripts_that_are_not_named_after_a_session(workbench_home: Path) -> None:
    """`claude --resume` only accepts a uuid, so anything else in the directory is not a candidate."""
    cwd = workbench_home / "ws"
    _write_transcript(cwd, ONE, mtime=1000)
    stray = transcript_dir(cwd) / "notes.jsonl"
    stray.write_text("{}\n")
    os.utime(stray, (5000, 5000))
    assert latest_session_id(cwd) == ONE


def test_a_transcript_that_vanishes_mid_scan_reads_as_no_session(
    workbench_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude owns this directory and prunes it while the workbench is reading it.

    Losing the lookup costs a fresh conversation; raising here would cost the whole tab, which is
    the worse trade — so the scan swallows the race rather than propagating it.
    """
    cwd = workbench_home / "ws"
    _write_transcript(cwd, ONE, mtime=1000)

    def vanished(self: Path, *args: object, **kwargs: object) -> object:
        raise FileNotFoundError(self)

    monkeypatch.setattr(Path, "stat", vanished)
    assert latest_session_id(cwd) == ""
