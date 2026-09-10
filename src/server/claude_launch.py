import shlex


def claude_session_command(claude_bin: str, session_id: str, *, resume_first: bool, prompt: str = "") -> str:
    """Shell snippet that lands the user in `session_id`, whether or not it exists yet.

    Both orderings work — the loser of the pair just exits non-zero — so the order only decides
    whether the user sees a spurious error first. Lead with resume when the session is expected to
    exist (a restore) and with create when it isn't (a brand new tab).

    `prompt` starts the conversation already working on something. It is passed positionally, and
    quoted rather than interpolated because it carries text from outside the workbench — the title
    and description of whatever issue asked for the work.
    """
    claude = shlex.quote(claude_bin)
    sid = shlex.quote(session_id)
    tail = f" {shlex.quote(prompt)}" if prompt else ""
    create = f"{claude} --session-id {sid} --dangerously-skip-permissions{tail}"
    resume = f"{claude} --resume {sid} --dangerously-skip-permissions{tail}"
    return f"{resume} || {create}" if resume_first else f"{create} || {resume}"
