import datetime
import signal
from types import FrameType

HANGUP = signal.SIGHUP


def _log_hangup(signum: int, frame: FrameType | None) -> None:
    when = datetime.datetime.now(tz=datetime.UTC).isoformat(timespec="seconds")
    print(f"[signals] {when} ignored a SIGHUP", flush=True)


def survive_hangups() -> None:
    """Stop SIGHUP from killing the workbench, and leave a trace when one arrives.

    This app runs arbitrary code inside its own container by design — Claude, the user's shells,
    its own test suite — and any of it can signal the server or pid 1. Python's default action for
    SIGHUP is to die, so one stray `kill -HUP` took down every terminal in every workspace at once.
    Nothing legitimate hangs up a containerised server: openhost stops us with SIGTERM and the
    reload path with SIGKILL.

    A handler rather than a mask, because a mask is per-thread: a process-directed signal is
    delivered to any thread that doesn't block it, and hypercorn's threads don't all inherit ours,
    so blocking it in the main thread let SIGHUP through to a thread that still died of it. A
    handler is a property of the process, so whichever thread takes the signal, the process lives.
    """
    signal.signal(HANGUP, _log_hangup)


def reset_child_signals() -> None:
    """Put the default disposition back in a forked child, before it execs.

    exec already resets a *handled* signal to its default, but not an ignored one, and the
    entrypoint ignores SIGHUP so that pid 1 survives it too — which every child inherits. Without
    this, no terminal could ever be hung up again, kill_tab() included.
    """
    signal.signal(HANGUP, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {HANGUP})
