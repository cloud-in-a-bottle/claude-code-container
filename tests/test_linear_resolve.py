from __future__ import annotations

import pytest

from server.linear.api import LinearIssue
from server.linear.naming import branch_name
from server.linear.naming import workspace_base_name
from server.linear.repos import GithubRepo
from server.linear.repos import OrgRepos
from server.linear.resolve import RepoResolutionError
from server.linear.resolve import build_prompt
from server.linear.resolve import parse_choice

REPOS = OrgRepos(
    org="cloud-in-a-bottle",
    repos=(
        GithubRepo(name="md-notes", description="collaborative markdown notes", is_private=False),
        GithubRepo(name="backup", description="back your instance up", is_private=False),
        GithubRepo(name="Cap", description="screen recordings", is_private=False),
    ),
)

ISSUE = LinearIssue(
    id="issue-1",
    identifier="ENG-7",
    title="Notes editor drops the last character",
    description="Typing fast loses a keystroke.",
    labels=("notes",),
    url="https://linear.app/x/issue/ENG-7",
    team_key="ENG",
    comments=("zack: @claude take this",),
)


class TestBranchName:
    def test_leads_with_the_issue_key_so_linear_links_the_pr(self) -> None:
        assert branch_name("ENG-123", "Fix the thing") == "eng-123-fix-the-thing"

    def test_drops_punctuation(self) -> None:
        assert branch_name("ENG-1", "Don't crash on empty input!") == "eng-1-don-t-crash-on-empty-input"

    def test_truncates_a_long_title(self) -> None:
        assert branch_name("ENG-1", "a b c d e f g h i") == "eng-1-a-b-c-d-e-f"

    def test_an_empty_title_leaves_just_the_key(self) -> None:
        assert branch_name("ENG-1", "") == "eng-1"

    def test_workspace_is_named_for_the_issue(self) -> None:
        assert workspace_base_name("ENG-123") == "ENG-123"


class TestParseChoice:
    def test_reads_a_plain_json_reply(self) -> None:
        choice = parse_choice(
            '{"repo": "md-notes", "confidence": 0.9, "alternatives": [], "reasoning": "notes label"}', REPOS
        )
        assert (choice.repo, choice.confidence, choice.reasoning) == ("md-notes", 0.9, "notes label")

    def test_digs_the_json_out_of_surrounding_prose(self) -> None:
        reply = 'Sure!\n```json\n{"repo": "backup", "confidence": 0.8}\n```\nHope that helps.'
        assert parse_choice(reply, REPOS).repo == "backup"

    def test_matches_a_repo_name_case_insensitively(self) -> None:
        """The resolver is a language model; `cap` for `Cap` is the right answer spelled loosely."""
        assert parse_choice('{"repo": "cap", "confidence": 0.9}', REPOS).repo == "Cap"

    def test_accepts_an_org_qualified_name(self) -> None:
        assert parse_choice('{"repo": "cloud-in-a-bottle/backup", "confidence": 0.9}', REPOS).repo == "backup"

    def test_rejects_a_repo_that_does_not_exist(self) -> None:
        """A hallucinated name would otherwise become a clone URL that 404s mid-workspace."""
        with pytest.raises(RepoResolutionError, match="not a repo"):
            parse_choice('{"repo": "md-notes-v2", "confidence": 0.99}', REPOS)

    def test_drops_alternatives_that_do_not_exist(self) -> None:
        choice = parse_choice('{"repo": "md-notes", "confidence": 0.5, "alternatives": ["backup", "nope"]}', REPOS)
        assert choice.alternatives == ("backup",)

    def test_an_unreadable_confidence_counts_as_no_confidence(self) -> None:
        assert parse_choice('{"repo": "md-notes", "confidence": "very"}', REPOS).confidence == 0.0

    def test_a_missing_confidence_counts_as_no_confidence(self) -> None:
        assert parse_choice('{"repo": "md-notes"}', REPOS).confidence == 0.0

    def test_rejects_a_reply_with_no_json_at_all(self) -> None:
        with pytest.raises(RepoResolutionError, match="could not find JSON"):
            parse_choice("I think it's probably the notes one", REPOS)

    def test_rejects_malformed_json(self) -> None:
        with pytest.raises(RepoResolutionError, match="not valid JSON"):
            parse_choice('{"repo": "md-notes",}', REPOS)


class TestPrompt:
    def test_offers_every_repo_with_its_description(self) -> None:
        prompt = build_prompt(ISSUE, REPOS)
        assert "- md-notes: collaborative markdown notes" in prompt
        assert "- Cap: screen recordings" in prompt

    def test_includes_the_labels_it_is_told_to_weigh(self) -> None:
        assert "Labels: notes" in build_prompt(ISSUE, REPOS)
