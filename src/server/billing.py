"""How a workspace's Claude sessions are paid for: an Anthropic API key, or a Claude subscription.

The two are mutually exclusive per process, and Claude Code decides between them from its
environment: with ANTHROPIC_API_KEY set it bills the key and ignores the OAuth credentials
`claude auth login` leaves in ~/.claude, so subscription mode means *removing* the key rather than
merely not adding one. That asymmetry is why an env overlay here can carry a None (see
apply_env_overlay) instead of being a plain dict of strings.

The choice is made per workspace, when the workspace is created, and pinned from then on: a
workspace is where the work and the conversations live, so changing the default must not quietly
move existing ones onto the other payment method.
"""

import asyncio
import json
import os
import shutil
from collections.abc import Mapping
from collections.abc import MutableMapping
from typing import Any

import attr

from server.config import STATE_DIR
from server.remote_services import get_anthropic_key

# Under $HOME, which openhost points at the app's persistent data dir, so the choice survives an
# image rebuild along with the workspaces it describes.
BILLING_PATH = STATE_DIR / "billing.json"

# The modes. These are written to disk and sent over the API, so they are a wire format.
API = "api"
SUBSCRIPTION = "subscription"
MODES = (API, SUBSCRIPTION)
# API billing is the default because it needs no interactive setup: the key arrives from the
# secrets app on its own, while a subscription has to be logged into from inside the container.
DEFAULT_MODE = API

API_KEY_VAR = "ANTHROPIC_API_KEY"

# `claude auth status` is a node process doing local work, so it is quick; the bound is only there
# so a wedged binary can't hang the settings page.
AUTH_STATUS_TIMEOUT_SECONDS = 15.0


@attr.s(auto_attribs=True, frozen=True)
class Billing:
    """The default for new workspaces, plus the mode each existing workspace was created with."""

    default_mode: str = DEFAULT_MODE
    # Keyed by workspace id (`<project>/<workspace>`).
    workspaces: Mapping[str, str] = attr.Factory(dict)


def _check_mode(mode: str, where: str = "") -> None:
    if mode not in MODES:
        suffix = f" in {where}" if where else ""
        raise ValueError(f"unknown billing mode {mode!r}{suffix}; expected one of: {', '.join(MODES)}")


def load_billing() -> Billing:
    """Read the saved billing choices, falling back to defaults only when nothing is saved yet.

    A malformed file raises rather than reverting to defaults. Guessing here would silently bill
    the wrong account, which is worse to discover later than a loud error is now. Nothing on the
    startup path reads this, so a bad file can't stop the workbench from booting — it fails the
    requests that need it, which is where it can be seen and fixed.
    """
    if not BILLING_PATH.exists():
        return Billing()
    raw = json.loads(BILLING_PATH.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"malformed billing settings at {BILLING_PATH}: expected an object")

    # .get() so a file written by an older build still loads.
    default_mode = str(raw.get("default_mode", DEFAULT_MODE))
    _check_mode(default_mode, str(BILLING_PATH))

    saved = raw.get("workspaces", {})
    if not isinstance(saved, dict):
        raise ValueError(f"malformed billing settings at {BILLING_PATH}: workspaces must be an object")
    workspaces: dict[str, str] = {}
    for workspace_id, value in saved.items():
        mode = str(value)
        _check_mode(mode, f"{BILLING_PATH} (workspace {workspace_id})")
        workspaces[str(workspace_id)] = mode

    return Billing(default_mode=default_mode, workspaces=workspaces)


def save_billing(billing: Billing) -> None:
    """Persist via a temp file + rename, so an interrupted write can't corrupt the file."""
    _check_mode(billing.default_mode)
    for workspace_id, mode in billing.workspaces.items():
        _check_mode(mode, f"workspace {workspace_id}")
    BILLING_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"default_mode": billing.default_mode, "workspaces": dict(billing.workspaces)}
    tmp_path = BILLING_PATH.with_name(BILLING_PATH.name + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2) + "\n")
    tmp_path.replace(BILLING_PATH)


def mode_of(billing: Billing, workspace_id: str) -> str:
    """The mode a workspace's terminals run on: what it was created with, else today's default.

    The fallback covers workspaces that predate this setting, and ones whose entry was edited out
    of the file by hand. Everything created since is pinned at creation.
    """
    return billing.workspaces.get(workspace_id, billing.default_mode)


def workspace_mode(workspace_id: str) -> str:
    return mode_of(load_billing(), workspace_id)


def default_mode() -> str:
    return load_billing().default_mode


def set_default_mode(mode: str) -> None:
    """Change what new workspaces get. Existing workspaces keep the mode they were created with."""
    _check_mode(mode)
    billing = load_billing()
    save_billing(attr.evolve(billing, default_mode=mode))


def pin_workspace_mode(workspace_id: str, mode: str) -> None:
    """Record the mode a workspace was created with — called once, at creation."""
    _check_mode(mode)
    billing = load_billing()
    save_billing(attr.evolve(billing, workspaces={**billing.workspaces, workspace_id: mode}))


def forget_workspace(workspace_id: str) -> None:
    """Drop a deleted workspace's entry, so the file doesn't accumulate rows for dead workspaces."""
    billing = load_billing()
    if workspace_id not in billing.workspaces:
        return
    remaining = {k: v for k, v in billing.workspaces.items() if k != workspace_id}
    save_billing(attr.evolve(billing, workspaces=remaining))


async def auth_env(mode: str) -> dict[str, str | None]:
    """The environment changes that put a child process on `mode`.

    A None value means "remove this variable from whatever the child would have inherited".
    Subscription mode needs that, not just the absence of a key: the workbench's own environment
    may well carry an ANTHROPIC_API_KEY (the openhost runtime can inject one, and users export
    them by hand), and Claude Code prefers a key over stored subscription credentials — so a stray
    one would bill the API account while the UI said "subscription".
    """
    if mode == SUBSCRIPTION:
        return {API_KEY_VAR: None}
    key = await get_anthropic_key()
    # No key from the secrets app leaves whatever the environment already has, which is how a
    # hand-exported key keeps working.
    return {API_KEY_VAR: key} if key else {}


async def api_key_available() -> bool:
    """Whether API billing has anything to bill to — asked by the settings page.

    Goes through the same lookup the tabs use, so the page can't say "available" about a key a
    workspace would not actually get.
    """
    return bool(await get_anthropic_key())


async def workspace_auth_env(workspace_id: str) -> dict[str, str | None]:
    """The auth environment for anything started inside a workspace — terminals, editors, scripts.

    Applied to plain shell tabs as well as Claude ones: a shell is a place someone runs `claude`
    by hand, and it should be paid for the same way as the workspace it sits in.
    """
    return await auth_env(workspace_mode(workspace_id))


def apply_env_overlay(env: MutableMapping[str, str], overlay: Mapping[str, str | None]) -> None:
    """Apply an overlay to a concrete environment: None removes a variable, anything else sets it.

    One place defines the None-means-unset rule, because both kinds of child process the workbench
    starts (pty tabs and code-server) need it and would otherwise each invent their own.
    """
    for name, value in overlay.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value


def _not_signed_in(detail: str) -> dict[str, Any]:
    return {"logged_in": False, "method": "", "account": "", "detail": detail}


def parse_auth_status(stdout: bytes, stderr: bytes = b"") -> dict[str, Any]:
    """Read `claude auth status --json` output into the answer the settings page wants.

    Pure, and forgiving: the CLI's schema is its own and can grow fields or change wording, so
    anything unrecognisable becomes "not signed in" with the output as the explanation rather than
    an exception in a page whose job is to explain auth problems.
    """
    try:
        raw = json.loads(stdout.decode(errors="replace"))
    except ValueError:
        said = stderr.decode(errors="replace").strip() or stdout.decode(errors="replace").strip()
        return _not_signed_in(said[:200] or "`claude auth status` said nothing")
    if not isinstance(raw, dict):
        return _not_signed_in("`claude auth status` returned an unexpected shape")

    method = str(raw.get("authMethod", "") or "")
    # An api_key answer despite the stripped variable means a key is reaching Claude Code from
    # somewhere else (an apiKeyHelper in ~/.claude/settings.json, say) — not a subscription.
    logged_in = bool(raw.get("loggedIn")) and method != "api_key"
    # Best-effort: which account is signed in, when the CLI volunteers it under a name we know.
    account = str(raw.get("email") or raw.get("account") or raw.get("organization") or "")
    return {"logged_in": logged_in, "method": method, "account": account, "detail": ""}


async def subscription_status() -> dict[str, Any]:
    """Whether a Claude subscription has been logged into in this container.

    Probed with `claude auth status --json`, and with ANTHROPIC_API_KEY stripped from the
    environment: with a key present Claude Code reports the key and says nothing about the stored
    subscription credentials, which is exactly the question being asked here.

    Never raises, for the same reason parse_auth_status() is forgiving.
    """
    claude = shutil.which("claude") or "claude"
    env = {k: v for k, v in os.environ.items() if k != API_KEY_VAR}
    try:
        proc = await asyncio.create_subprocess_exec(
            claude,
            "auth",
            "status",
            "--json",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        return _not_signed_in(f"could not run `{claude} auth status`: {e}")

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=AUTH_STATUS_TIMEOUT_SECONDS)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return _not_signed_in("`claude auth status` timed out")

    return parse_auth_status(stdout, stderr)
