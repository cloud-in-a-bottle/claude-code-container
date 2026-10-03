from server.linear.api import LinearIssue
from server.linear.config import workspace_url


def build_run_prompt(issue: LinearIssue, instructions: str, branch: str, workspace_id: str) -> str:
    """The opening message Claude gets in a freshly cloned workspace.

    Handed to `claude` as its positional prompt, so the conversation starts already working. The
    tab stays interactive and attached, which is the point: the run is watchable from the sidebar
    and can be taken over mid-task rather than being a black box that either produces a PR or
    doesn't.
    """
    labels = ", ".join(issue.labels) or "(none)"
    extra = f"\n\nThey added, in the comment that triggered this:\n{instructions}\n" if instructions else "\n"

    return f"""You are working on a Linear issue. This workspace is a fresh clone and you are
already on the branch `{branch}`, created for this issue.

{issue.identifier}: {issue.title}
{issue.url}
Labels: {labels}

{issue.description or "(no description)"}
{extra}
Do the work, then ship it:

1. Implement the change. Follow the conventions already in this repo, and read its README and any
   CLAUDE.md before you start.
2. Run whatever tests or checks the repo has. If something was already broken before you touched
   it, say so in the PR rather than trying to fix everything.
3. Commit on `{branch}` and push it.
4. Open a PR with `gh pr create`, ready for review rather than draft. End the PR body with these
   two lines exactly:

   Linear issue: {issue.url}
   Workspace: {workspace_url(workspace_id)}

5. Comment on the issue so it's known the PR is up:

   linear comment {issue.identifier} --agent "PR is open: <the PR url> — <one line on what you did>"

   `--agent` marks the comment as machine-written. Keep it on: the comment posts under the owner's
   own Linear account, so without it a reader sees their name and avatar on something they did not
   write. `linear` is on PATH in this container and needs no credentials or setup; it reaches
   Linear through latchkey. Run `linear --help` if you need the other subcommands.

If you cannot do the work — the issue is ambiguous, the repo is the wrong one, the change needs a
decision only a person can make — do not guess and do not open a PR. Use
`linear comment {issue.identifier} --agent` to say what is blocking you, and stop. A question in
Linear is a good outcome; a plausible-looking PR that solves the wrong problem is not."""
