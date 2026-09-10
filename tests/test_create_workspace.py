from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest

from server.claude_launch import claude_session_command
from server.projects.create_workspace import authed_url
from server.projects.create_workspace import main
from server.projects.launch import bootstrap_command


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A real repo to clone, with a second branch and a commit on main."""
    repo = tmp_path / "origin"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "first", cwd=repo)
    _git("branch", "existing", cwd=repo)
    return repo


@pytest.fixture
def bootstrap_env(tmp_path: Path, origin: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the bootstrap at that repo, and return the workspace path it will fill in."""
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("WS_PATH", str(workspace))
    monkeypatch.setenv("WS_REPO", str(origin))
    monkeypatch.setenv("WS_MIRROR", str(tmp_path / "mirrors" / "origin.git"))
    for name in ("WS_REF", "WS_BRANCH", "WS_SETUP", "WS_GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    return workspace


class TestAuthedUrl:
    def test_embeds_the_token_in_an_https_url(self) -> None:
        assert authed_url("https://github.com/o/r.git", "tok") == "https://tok@github.com/o/r.git"

    def test_percent_encodes_a_token_with_url_characters(self) -> None:
        """An unencoded `/` or `@` in a token would otherwise be read as part of the URL."""
        assert authed_url("https://github.com/o/r.git", "a/b@c") == "https://a%2Fb%40c@github.com/o/r.git"

    def test_leaves_ssh_alone(self) -> None:
        assert authed_url("git@github.com:o/r.git", "tok") == "git@github.com:o/r.git"

    def test_no_token_changes_nothing(self) -> None:
        assert authed_url("https://github.com/o/r.git", "") == "https://github.com/o/r.git"


class TestBootstrapCommand:
    def test_runs_through_a_login_shell(self) -> None:
        """That is where this container's PATH additions come from, and Claude inherits them."""
        assert bootstrap_command("/bin/claude", "sid")[:3] == ["bash", "-l", "-c"]

    def test_drops_the_github_token_before_the_user_can_type(self) -> None:
        script = bootstrap_command("/bin/claude", "sid")[3]
        assert "unset WS_GITHUB_TOKEN" in script
        assert script.index("unset WS_GITHUB_TOKEN") < script.index("exec bash")

    def test_claude_only_starts_if_the_bootstrap_succeeded(self) -> None:
        script = bootstrap_command("/bin/claude", "sid")[3]
        assert "status=$?" in script and '[ "$status" = 0 ]' in script

    def test_always_leaves_a_shell_behind(self) -> None:
        assert bootstrap_command("/bin/claude", "sid")[3].endswith("exec bash")

    def test_carries_the_opening_prompt(self) -> None:
        assert "'do the thing'" in bootstrap_command("/bin/claude", "sid", "do the thing")[3]


class TestClaudeSessionCommand:
    def test_a_new_tab_tries_to_create_the_session_first(self) -> None:
        command = claude_session_command("/bin/claude", "sid", resume_first=False)
        assert command.index("--session-id") < command.index("--resume")

    def test_a_restore_tries_to_resume_first(self) -> None:
        command = claude_session_command("/bin/claude", "sid", resume_first=True)
        assert command.index("--resume") < command.index("--session-id")

    def test_the_prompt_goes_to_both_halves_of_the_fallback(self) -> None:
        """Whichever of create/resume wins has to be the one that gets the work."""
        command = claude_session_command("/bin/claude", "sid", resume_first=False, prompt="do the thing")
        assert [shlex.split(half)[-1] for half in command.split(" || ")] == ["do the thing", "do the thing"]

    def test_a_prompt_arrives_as_one_argument_however_it_is_written(self) -> None:
        """Prompts carry issue text from outside the workbench, so they are quoted, not trusted.
        Splitting the snippet the way a shell would must give the prompt back whole."""
        nasty = '\'; rm -rf / ; echo "pwned" #'
        command = claude_session_command("/bin/claude", "sid", resume_first=False, prompt=nasty)
        assert shlex.split(command.split(" || ")[0])[-1] == nasty


class TestBuildingAWorkspace:
    def test_clones_the_repo_into_the_workspace(self, bootstrap_env: Path) -> None:
        assert main() == 0
        assert (bootstrap_env / "README.md").read_text() == "hello\n"

    def test_origin_points_at_the_real_remote_not_the_mirror(self, bootstrap_env: Path, origin: Path) -> None:
        """Otherwise push and pull in the workspace would quietly talk to a local bare repo."""
        assert main() == 0
        assert _git("remote", "get-url", "origin", cwd=bootstrap_env) == str(origin)

    def test_checks_out_the_requested_ref(self, bootstrap_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WS_REF", "existing")
        assert main() == 0
        assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=bootstrap_env) == "existing"

    def test_a_bad_ref_still_leaves_a_usable_workspace(
        self, bootstrap_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed checkout is a warning, not a reason to deny the user a workspace."""
        monkeypatch.setenv("WS_REF", "no-such-branch")
        assert main() == 0
        assert (bootstrap_env / "README.md").is_file()

    def test_creates_the_branch_a_run_will_open_its_pr_from(
        self, bootstrap_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WS_BRANCH", "eng-7-fix")
        assert main() == 0
        assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=bootstrap_env) == "eng-7-fix"

    def test_joins_a_branch_that_already_exists(self, bootstrap_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WS_BRANCH", "existing")
        assert main() == 0
        assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=bootstrap_env) == "existing"

    def test_runs_the_project_setup_command(self, bootstrap_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WS_SETUP", "echo ran > setup-ran")
        assert main() == 0
        assert (bootstrap_env / "setup-ran").is_file()

    def test_a_failing_setup_command_does_not_deny_the_workspace(
        self, bootstrap_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WS_SETUP", "exit 3")
        assert main() == 0

    def test_a_second_workspace_reuses_the_mirror(
        self, bootstrap_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The first workspace pays for the network clone; every one after is local."""
        assert main() == 0
        second = tmp_path / "workspace-2"
        monkeypatch.setenv("WS_PATH", str(second))
        assert main() == 0
        assert (second / "README.md").is_file()

    def test_an_unreachable_repo_is_fatal(self, bootstrap_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-zero is how the tab is told to leave the user in a shell instead of starting Claude."""
        monkeypatch.setenv("WS_REPO", str(bootstrap_env.parent / "nope"))
        assert main() != 0

    def test_a_failed_mirror_clone_leaves_nothing_behind(
        self, bootstrap_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A half-made mirror would be reused by the next workspace and fail it too."""
        monkeypatch.setenv("WS_REPO", str(tmp_path / "nope"))
        assert main() != 0
        assert not (tmp_path / "mirrors" / "origin.git").exists()
