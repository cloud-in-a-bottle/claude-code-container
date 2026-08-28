import signal
import sys
import threading

HANGUP = signal.SIGHUP


def _describe_sender(pid: int) -> str:
    """A best-effort name for the process that sent us a signal."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read().replace(b"\x00", b" ").decode(errors="replace").strip()
    except OSError:
        return "<gone>"
    return cmdline or "<unknown>"


def survive_hangups() -> None:
    """Stop SIGHUP from killing the workbench, and record who sent it.

    This app runs arbitrary code inside its own container by design — Claude, the user's shells,
    its own test suite — and any of it can signal pid 1, which tini forwards straight to us.
    Python's default action for SIGHUP is to die, so one stray `kill -HUP` anywhere inside takes
    down every terminal in every workspace at once. Nothing legitimate hangs up a containerised
    server: openhost stops us with SIGTERM and the reload path with SIGKILL.

    Call before starting any thread — the mask is what threads inherit — and note that children
    must undo it in reset_child_signals(), or no tab could ever be hung up again.
    """
    if sys.platform != "linux":
        # macOS has no sigwaitinfo, so a local `just run` gets the immunity without the diagnostic.
        signal.signal(HANGUP, signal.SIG_IGN)
        return

    signal.pthread_sigmask(signal.SIG_BLOCK, {HANGUP})

    def watch() -> None:
        while True:
            info = signal.sigwaitinfo({HANGUP})
            sender = _describe_sender(info.si_pid)
            print(
                f"[signals] ignored SIGHUP from pid={info.si_pid} uid={info.si_uid}: {sender}",
                flush=True,
            )

    threading.Thread(target=watch, name="sighup-watch", daemon=True).start()


def reset_child_signals() -> None:
    """Undo survive_hangups() in a forked child, before it execs.

    Both a blocked signal and SIG_IGN survive exec, so without this every terminal would inherit a
    shell that cannot be hung up — including by kill_tab(), whose SIGHUP would land on nothing.
    """
    signal.signal(HANGUP, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {HANGUP})
