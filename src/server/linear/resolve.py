import asyncio
import json
import re
import shutil
import subprocess

import attr

from server.linear.api import LinearIssue
from server.linear.repos import OrgRepos

_RESOLVE_TIMEOUT_SECONDS = 180
# The model's reply, pulled out of whatever prose it wrapped around it.
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


class RepoResolutionError(Exception):
    pass


@attr.s(auto_attribs=True, frozen=True)
class RepoChoice:
    repo: str
    confidence: float
    alternatives: tuple[str, ...]
    reasoning: str
    # A few words naming the work, for the branch and workspace name. Asked for here rather than in
    # a call of its own: this one has already read the issue, so the answer is nearly free.
    slug: str = ""


def build_prompt(issue: LinearIssue, repos: OrgRepos) -> str:
    catalogue = "\n".join(f"- {r.name}: {r.description or '(no description)'}" for r in repos.repos)
    labels = ", ".join(issue.labels) or "(none)"
    # Only the first few comments: the thread is context for *which repo*, and a long discussion
    # pushes the catalogue out of the model's attention for no gain on that specific question.
    thread = "\n\n".join(issue.comments[:5]) or "(no comments)"
    return f"""You are reading a Linear issue to decide two things: which repository in the GitHub
organisation `{repos.org}` the work belongs in, and what to call the branch it will be done on.

Issue {issue.identifier}: {issue.title}
Labels: {labels}

Description:
{issue.description or "(none)"}

Comment thread:
{thread}

Repositories available:
{catalogue}

Reply with ONLY a JSON object, no prose and no code fence:
{{"repo": "<exact repo name from the list>",
  "confidence": <0.0 to 1.0>,
  "alternatives": ["<other plausible repo names>"],
  "reasoning": "<one sentence>",
  "slug": "<3-5 words naming the task, lower-case, hyphen-separated>"}}

The slug names the *work*, not the issue. For "Add a doc about our managed spaces" write
`add-managed-spaces-docs`, not `add-a-doc-about-our` and not `cb-295`. Drop filler words, keep the
words someone would recognise the change by, and leave the issue key out of it -- it gets added
for you.

The labels usually name the area of the project the issue belongs to, so weigh them heavily.
Set confidence below 0.6 if the issue could plausibly belong to more than one repo, or if nothing
in the list is a good fit — a wrong repo wastes a whole run, so say you are unsure instead of
guessing. Never invent a repo name that is not in the list."""


async def choose_repo(issue: LinearIssue, repos: OrgRepos) -> RepoChoice:
    """Ask Claude which repo an issue belongs to.

    Shelled out to the `claude` CLI rather than called over the API because the CLI uses whatever
    authentication this workbench already has — an interactive `claude login` as readily as an
    `ANTHROPIC_API_KEY` — and there is no second credential to keep working.
    """
    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise RepoResolutionError("the claude CLI is not on PATH; cannot resolve the repo")

    proc = await asyncio.create_subprocess_exec(
        claude_bin,
        "-p",
        build_prompt(issue, repos),
        "--output-format",
        "text",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_RESOLVE_TIMEOUT_SECONDS)
    except TimeoutError:
        proc.kill()
        raise RepoResolutionError("timed out asking Claude which repo this issue belongs to") from None
    if proc.returncode:
        raise RepoResolutionError(f"claude failed to resolve the repo: {err.decode(errors='replace').strip()[:300]}")

    return parse_choice(out.decode(errors="replace"), repos)


def parse_choice(reply: str, repos: OrgRepos) -> RepoChoice:
    """Read the model's JSON reply, rejecting a repo that isn't really in the org.

    The name check is the important half: a hallucinated repo would otherwise become a clone URL
    that 404s halfway through creating a workspace, which is a far more confusing failure than
    being told the resolver picked something that doesn't exist.
    """
    match = _JSON_RE.search(reply)
    if not match:
        raise RepoResolutionError(f"could not find JSON in the resolver's reply: {reply.strip()[:300]}")
    try:
        parsed = json.loads(match.group(0))
    except ValueError as e:
        raise RepoResolutionError(f"resolver reply was not valid JSON: {match.group(0)[:300]}") from e
    if not isinstance(parsed, dict):
        raise RepoResolutionError("resolver reply was not a JSON object")

    name = str(parsed.get("repo") or "")
    found = repos.find(name)
    if found is None:
        raise RepoResolutionError(f"the resolver chose {name!r}, which is not a repo in {repos.org}")

    raw_alternatives = parsed.get("alternatives")
    alternatives = (
        tuple(str(a) for a in raw_alternatives if isinstance(a, str) and repos.find(a) is not None)
        if isinstance(raw_alternatives, list)
        else ()
    )

    try:
        confidence = float(parsed.get("confidence", 0.0))
    except TypeError, ValueError:
        confidence = 0.0

    return RepoChoice(
        repo=found.name,
        confidence=confidence,
        alternatives=alternatives,
        reasoning=str(parsed.get("reasoning") or ""),
        # Left as it came back; `naming` is the one place that decides what a usable slug is, and
        # it has to sanitise the title fallback anyway.
        slug=str(parsed.get("slug") or ""),
    )
