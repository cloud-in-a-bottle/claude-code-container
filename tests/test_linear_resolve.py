from __future__ import annotations

import pytest

from server.linear.api import LinearIssue
from server.linear.naming import descriptor_slug
from server.linear.naming import run_name
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


class TestRunName:
    def test_reads_as_the_task_not_the_title(self) -> None:
        """The whole point: `add-managed-spaces-docs` beats the title's first five words."""
        assert run_name("CB-295", "add-managed-spaces-docs", "Add a doc about our managed spaces") == (
            "cb-295-add-managed-spaces-docs"
        )

    def test_leads_with_the_issue_key_so_linear_links_the_pr(self) -> None:
        assert run_name("ENG-123", "fix-the-thing").startswith("eng-123-")

    def test_falls_back_to_the_title_when_no_descriptor_came_back(self) -> None:
        assert run_name("ENG-1", "", "Fix the thing") == "eng-1-fix-the-thing"

    def test_with_neither_it_is_still_a_usable_name(self) -> None:
        assert run_name("ENG-1", "", "") == "eng-1"

    def test_a_descriptor_written_as_prose_is_still_usable(self) -> None:
        """Models don't always honour "hyphen-separated"."""
        assert run_name("ENG-1", "Add managed spaces docs") == "eng-1-add-managed-spaces-docs"

    def test_drops_punctuation(self) -> None:
        assert run_name("ENG-1", "don't crash on empty input!") == "eng-1-don-t-crash-on-empty"

    def test_caps_a_rambling_descriptor(self) -> None:
        """A name has to stay readable in the sidebar and short enough to be a legal directory."""
        name = run_name("ENG-1", "a b c d e f g h i j k l m n o p")
        assert name == "eng-1-a-b-c-d-e"

    def test_trims_a_name_that_got_cut_on_a_filler_word(self) -> None:
        """`cb-295-add-a-doc-about-our` was a real branch name, and reads as if it got cut off."""
        assert run_name("CB-295", "", "Add a doc about our managed spaces") == "cb-295-add-a-doc"

    def test_never_trims_away_the_only_word(self) -> None:
        assert run_name("ENG-1", "", "The") == "eng-1-the"

    def test_a_descriptor_of_only_punctuation_falls_back(self) -> None:
        assert run_name("ENG-1", "!!! ???", "Fix the thing") == "eng-1-fix-the-thing"

    def test_the_issue_key_is_lowercased_for_git(self) -> None:
        """Refs are case-sensitive and people are not, so the name is lower-case throughout."""
        assert run_name("CB-295", "docs") == "cb-295-docs"


class TestDescriptorSlug:
    def test_prefers_the_descriptor(self) -> None:
        assert descriptor_slug("add-managed-spaces-docs", "Some Title") == "add-managed-spaces-docs"

    def test_uses_the_title_only_when_the_descriptor_is_unusable(self) -> None:
        assert descriptor_slug("", "Some Title") == "some-title"


class TestParseChoice:
    def test_reads_a_plain_json_reply(self) -> None:
        choice = parse_choice(
            '{"repo": "md-notes", "confidence": 0.9, "alternatives": [], "reasoning": "notes label",'
            ' "slug": "fix-editor-dropped-keystroke"}',
            REPOS,
        )
        assert (choice.repo, choice.confidence, choice.reasoning) == ("md-notes", 0.9, "notes label")
        assert choice.slug == "fix-editor-dropped-keystroke"

    def test_a_reply_without_a_slug_still_parses(self) -> None:
        """Naming falls back to the title, so a missing slug must not fail the whole resolution."""
        assert parse_choice('{"repo": "md-notes", "confidence": 0.9}', REPOS).slug == ""

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

    def test_asks_for_a_slug_naming_the_work(self) -> None:
        prompt = build_prompt(ISSUE, REPOS)
        assert '"slug"' in prompt
        assert "names the *work*, not the issue" in prompt
