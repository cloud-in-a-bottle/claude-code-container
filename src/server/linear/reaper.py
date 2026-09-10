import asyncio
import json
import subprocess

import attr

from server.linear.config import REAP_INTERVAL_SECONDS
from server.linear.runs import LinearRun
from server.linear.runs import load_runs
from server.linear.runs import remove_run
from server.projects.teardown import teardown_workspace
from server.projects.workspaces import parse_workspace_id

_GH_TIMEOUT_SECONDS = 30


@attr.s(auto_attribs=True, frozen=True)
class PullRequestState:
    #  "MERGED" | "CLOSED" | "OPEN" | "" when the branch has no PR yet
    state: str
    url: str


async def pull_request_for(run: LinearRun) -> PullRequestState:
    """The state of the PR opened from this run's branch, if there is one yet.

    `--state all` on purpose: a merged PR is not listed by the default `open` filter, which is the
    only state this is looking for.
    """
    proc = await asyncio.create_subprocess_exec(
        "gh",
        "pr",
        "list",
        "--repo",
        run.repo,
        "--head",
        run.branch,
        "--state",
        "all",
        "--limit",
        "1",
        "--json",
        "state,url",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_GH_TIMEOUT_SECONDS)
    except TimeoutError:
        proc.kill()
        raise RuntimeError(f"timed out looking for the PR on {run.repo}#{run.branch}") from None
    if proc.returncode:
        raise RuntimeError(f"gh could not list PRs on {run.repo}: {err.decode(errors='replace').strip()}")

    parsed = json.loads(out or b"[]")
    if not isinstance(parsed, list) or not parsed:
        return PullRequestState(state="", url="")
    first = parsed[0]
    if not isinstance(first, dict):
        return PullRequestState(state="", url="")
    return PullRequestState(state=str(first.get("state", "")), url=str(first.get("url", "")))


async def reap_merged_runs() -> None:
    """Delete the workspace behind every run whose PR has merged.

    Only MERGED counts. A closed-without-merging PR usually means the work was rejected and the
    workspace is exactly where you would go to understand why, so those are left alone — as is a
    run whose workspace someone already deleted by hand, which is just forgotten.
    """
    for run in load_runs():
        workspace = parse_workspace_id(run.workspace_id)
        if workspace is None or not workspace.path.is_dir():
            remove_run(run.workspace_id)
            continue
        try:
            pr = await pull_request_for(run)
        except Exception as e:
            print(f"[linear] could not check the PR for {run.issue_identifier}: {e}", flush=True)
            continue
        if pr.state != "MERGED":
            continue
        print(f"[linear] {run.issue_identifier} merged ({pr.url}); deleting workspace {run.workspace_id}", flush=True)
        await teardown_workspace(workspace)
        remove_run(run.workspace_id)


async def reap_periodically(interval_seconds: float = REAP_INTERVAL_SECONDS) -> None:
    """Run the reaper for the life of the process.

    Never lets an exception end the loop: this is a background task, so a raise here would stop
    reaping silently for the rest of the run and nobody would notice until workspaces piled up.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await reap_merged_runs()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[linear] reaper failed: {e}", flush=True)
