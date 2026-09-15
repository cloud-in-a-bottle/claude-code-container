from collections import deque

from server.billing import default_mode
from server.billing import pin_workspace_mode
from server.git_remote import resolve_access
from server.linear import api
from server.linear.config import GITHUB_ORG
from server.linear.config import MIN_REPO_CONFIDENCE
from server.linear.events import CommentEvent
from server.linear.events import instructions_from
from server.linear.naming import branch_name
from server.linear.naming import workspace_base_name
from server.linear.prompt import build_run_prompt
from server.linear.repos import list_org_repos
from server.linear.resolve import RepoChoice
from server.linear.resolve import choose_repo
from server.linear.runs import LinearRun
from server.linear.runs import add_run
from server.linear.runs import now_iso
from server.projects.launch import start_workspace_tab
from server.projects.store import add_project
from server.projects.store import find_project_by_repo
from server.projects.workspaces import Workspace
from server.projects.workspaces import create_workspace_dir
from server.projects.workspaces import unique_workspace_name

# Comment ids already acted on. Linear's delivery is at-least-once, and the same comment arriving
# twice would otherwise open two workspaces and two PRs. The route answers 200 before any of this
# runs, so a failure *inside* a run never provokes a redelivery -- this only has to catch a
# duplicate of a delivery this process already took. Bounded and in memory accordingly.
_HANDLED_COMMENTS: deque[str] = deque(maxlen=256)


class NotForUs(Exception):
    """The delivery was valid but isn't ours to act on. Not an error, and not reported to Linear."""


async def handle_comment(event: CommentEvent) -> None:
    """Turn a triggering comment into a workspace with Claude already working in it.

    Runs detached from the webhook response, because resolving a repo with an LLM and then cloning
    takes far longer than Linear will wait, and a delivery it considers timed out gets sent again.
    """
    if event.comment_id and event.comment_id in _HANDLED_COMMENTS:
        raise NotForUs(f"comment {event.comment_id} has already been handled")
    if event.comment_id:
        _HANDLED_COMMENTS.append(event.comment_id)

    # Only the owner's own comments start work. Fetched from Linear rather than configured, and a
    # failure here stops the run: "we couldn't tell who this was" must never mean "run it anyway",
    # since this endpoint is reachable by anyone who can forge a valid signature.
    if event.author_id != await api.viewer_id():
        raise NotForUs(f"comment author {event.author_id} is not the workbench owner")

    issue = await api.fetch_issue(event.issue_id)
    try:
        await _start_run(event, issue)
    except Exception as e:
        # The person is waiting in Linear, so failures are reported there rather than only in a log
        # they would have to know to go and read.
        await _report_failure(issue, e)
        raise


async def _start_run(event: CommentEvent, issue: api.LinearIssue) -> None:
    repos = await list_org_repos(GITHUB_ORG)
    choice = await choose_repo(issue, repos)

    if choice.confidence < MIN_REPO_CONFIDENCE:
        await api.post_comment(issue.id, _uncertain_comment(choice))
        print(
            f"[linear] {issue.identifier}: repo unclear ({choice.confidence:.2f}, best guess {choice.repo}); asked",
            flush=True,
        )
        return

    repo = repos.find(choice.repo)
    if repo is None:
        raise RuntimeError(f"resolver chose {choice.repo!r}, which vanished from the repo list")
    clone_url = repos.clone_url(repo)

    access = await resolve_access(clone_url, "HEAD")
    if access.decision != "ok":
        raise RuntimeError(f"cannot reach {clone_url}: {access.decision} ({access.detail})")

    project = find_project_by_repo(clone_url) or add_project(name=repo.name, repo_url=clone_url)
    workspace = Workspace(
        project_id=project.id,
        name=unique_workspace_name(project.id, workspace_base_name(issue.identifier)),
    )
    create_workspace_dir(workspace)
    # A run picks no billing mode of its own, so it gets the workbench default — pinned now,
    # like any other workspace, so a later change of default leaves this run alone.
    pin_workspace_mode(workspace.id, default_mode())

    branch = branch_name(issue.identifier, issue.title)
    prompt = build_run_prompt(issue, instructions_from(event.body), branch, workspace.id)
    await start_workspace_tab(
        project,
        workspace,
        ref=project.default_branch,
        github_token=access.token,
        branch=branch,
        prompt=prompt,
    )
    add_run(
        LinearRun(
            workspace_id=workspace.id,
            issue_id=issue.id,
            issue_identifier=issue.identifier,
            issue_url=issue.url,
            repo=f"{repos.org}/{repo.name}",
            branch=branch,
            created_at=now_iso(),
        )
    )
    print(f"[linear] {issue.identifier}: working in {workspace.id} on {branch}", flush=True)


def _uncertain_comment(choice: RepoChoice) -> str:
    candidates = [choice.repo, *choice.alternatives]
    listed = "\n".join(f"- `{c}`" for c in dict.fromkeys(candidates))
    return (
        "I'm not confident which repo this belongs in, so I haven't started anything.\n\n"
        f"Best guesses:\n{listed}\n\n"
        f"{choice.reasoning}\n\n"
        "Reply naming the repo and mention me again."
    )


async def _report_failure(issue: api.LinearIssue, failure: Exception) -> None:
    """Say in Linear that the run didn't start, and why.

    Best-effort by necessity — if Linear is what's broken, this is the call that will fail too —
    but the exception it swallows is the *reporting* failure, never the original one, which the
    caller re-raises to the log.
    """
    try:
        await api.post_comment(issue.id, f"I couldn't start work on this:\n\n```\n{failure}\n```")
    except Exception as e:
        print(f"[linear] could not report the failure on {issue.identifier}: {e}", flush=True)
