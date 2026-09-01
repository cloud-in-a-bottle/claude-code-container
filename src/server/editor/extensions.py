import asyncio
import json
from pathlib import Path

import attr

from server.editor import paths


@attr.s(auto_attribs=True, frozen=True)
class DefaultExtension:
    extension_id: str
    version: str

    @property
    def pinned(self) -> str:
        return f"{self.extension_id}@{self.version}"


# Pinned for the reason CODE_SERVER_VERSION is, and one step more necessary: extension auto-update
# is off (see DEFAULT_SETTINGS), so an unpinned install would settle on whatever happened to be
# latest the first time a given workbench started and then never move again. A pin makes the
# version a commit rather than an accident of when the container was first opened.
DEFAULT_EXTENSIONS = (
    # An extension pack: ms-python.debugpy and ms-python.vscode-python-envs are installed with it.
    # The third member of the pack is Pylance, which is Microsoft-licensed and not on Open VSX --
    # code-server skips the one it cannot find and installs the rest, and still exits 0.
    DefaultExtension("ms-python.python", "2026.4.0"),
    # The language server, standing in for the Pylance that pack asks for. Chosen over Pyright and
    # its forks on what a cold start costs here specifically: instances are capped at two and time
    # out after 30 minutes, so starting one is routine rather than a once-a-day event. Measured in
    # this container, pyrefly reaches first diagnostics in 0.2s and 104 MB where basedpyright takes
    # 3.0s and 326 MB, and it finds the same errors. It ships as a Rust binary inside the vsix, so
    # there is nothing to fetch on activation, and it reads [tool.mypy] out of pyproject.toml.
    DefaultExtension("meta.pyrefly", "1.2.0"),
    # Lint and format, matching what this repo's own pre-commit runs.
    DefaultExtension("charliermarsh.ruff", "2026.76.0"),
)

_INSTALL_TIMEOUT_SECONDS = 300.0

# Two workspaces opened at once would otherwise both install into the same shared directory.
_install_lock = asyncio.Lock()


def installed_marker_path() -> Path:
    return paths.VSCODE_DIR / "default-extensions.json"


def already_installed() -> set[str]:
    """The pinned ids this workbench has installed before.

    Recorded rather than read back off the extensions directory so that uninstalling one from the
    Extensions panel sticks. Probing the directory would reinstall it on the next start, which
    makes the panel's uninstall button quietly not work.
    """
    path = installed_marker_path()
    if not path.exists():
        return set()
    try:
        recorded = json.loads(path.read_text())
    except ValueError:
        # This file is ours, not the user's, so an unreadable one is a bug rather than something
        # to preserve -- and installing over the top of an extension already present is a no-op.
        print(f"[editor] {path} is not readable as JSON; treating the defaults as uninstalled", flush=True)
        return set()
    if not isinstance(recorded, list):
        return set()
    return {str(entry) for entry in recorded}


def _record_installed(pinned: str) -> None:
    path = installed_marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(already_installed() | {pinned}), indent=2) + "\n")


async def _install(binary: Path, extension: DefaultExtension) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            str(binary),
            "--extensions-dir",
            str(paths.EXTENSIONS_DIR),
            # The CLI keeps state of its own, and without somewhere to put it picks a directory
            # under ~/.local/share that nothing else here reads.
            "--user-data-dir",
            str(paths.CLI_DATA_DIR),
            "--config",
            str(paths.CONFIG_PATH),
            "--install-extension",
            extension.pinned,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as e:
        print(f"[editor] could not run the extension installer for {extension.pinned}: {e}", flush=True)
        return False
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), timeout=_INSTALL_TIMEOUT_SECONDS)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        print(f"[editor] timed out installing {extension.pinned}", flush=True)
        return False
    if proc.returncode != 0:
        print(f"[editor] could not install {extension.pinned}: {output.decode(errors='replace').strip()}", flush=True)
        return False
    return True


async def ensure_default_extensions(binary: Path) -> None:
    """Install the extensions a new workbench should already have, once, into the shared directory.

    Best-effort, which the rest of the editor code deliberately is not: Open VSX being unreachable
    should cost the user Python support, not the editor. Only what actually installed is recorded,
    so whatever failed is tried again the next time an editor starts.
    """
    async with _install_lock:
        missing = [e for e in DEFAULT_EXTENSIONS if e.pinned not in already_installed()]
        if not missing:
            return
        paths.EXTENSIONS_DIR.mkdir(parents=True, exist_ok=True)
        paths.CLI_DATA_DIR.mkdir(parents=True, exist_ok=True)
        # One at a time rather than one command with several --install-extension flags: a failure
        # partway through a batch still installs some of them, and this way each one is recorded
        # only if it is really there.
        for extension in missing:
            print(f"[editor] installing {extension.pinned}", flush=True)
            if await _install(binary, extension):
                _record_installed(extension.pinned)
