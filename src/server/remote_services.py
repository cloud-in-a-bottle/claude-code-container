import asyncio
import json
import os
import subprocess
import urllib.parse
from collections.abc import Mapping
from datetime import UTC
from datetime import datetime

import attr
import httpx
import tomli_w

from server.config import HOME
from server.config import STATE_DIR

ROUTER_URL = os.environ.get("OPENHOST_ROUTER_URL", "")
APP_TOKEN = os.environ.get("OPENHOST_APP_TOKEN", "")
SECRETS_SHORTNAME = "secrets"
OAUTH_SHORTNAME = "oauth"

GITHUB_SCOPES = ["repo"]
GH_HOSTS_PATH = HOME / ".config" / "gh" / "hosts.yml"
# Written next to hosts.yml only when *we* authenticated gh. Its absence next to an existing
# hosts.yml means the user logged in themselves, and we leave their credentials alone.
GH_MANAGED_MARKER = STATE_DIR / "gh-auth-managed"
# git's global config. Addressed explicitly rather than letting git find it, so that seeding an
# identity into it is redirectable in tests instead of landing in the real $HOME.
GIT_CONFIG_PATH = HOME / ".gitconfig"
# Fallback cadence when the token carries no expiry, and the ceiling on any refresh sleep. Minting
# is cheap, so refreshing more often than strictly needed costs little and keeps long-lived
# terminals working.
GH_REFRESH_MAX_SECONDS = 30 * 60
GH_REFRESH_MIN_SECONDS = 60
# Re-mint this long before the token actually expires, so a terminal never picks up a token that
# dies mid-command.
GH_REFRESH_SKEW_SECONDS = 5 * 60

_anthropic_key: str | None = None
_anthropic_lock = asyncio.Lock()
_github_account: str | None = None


@attr.s(auto_attribs=True, frozen=True)
class GithubToken:
    token: str
    account: str = ""
    expires_at: datetime | None = None

    def seconds_until_refresh(self) -> float:
        """How long this token can be relied on, minus a safety margin."""
        if self.expires_at is None:
            return GH_REFRESH_MAX_SECONDS
        remaining = (self.expires_at - datetime.now(UTC)).total_seconds() - GH_REFRESH_SKEW_SECONDS
        return max(GH_REFRESH_MIN_SECONDS, min(GH_REFRESH_MAX_SECONDS, remaining))


async def fetch_secrets(keys: list[str]) -> dict[str, str]:
    """Ask the secrets-v2 app for the given keys. Returns {} if unavailable."""
    if not ROUTER_URL or not APP_TOKEN:
        return {}
    url = f"{ROUTER_URL}/api/services/v2/call/{SECRETS_SHORTNAME}/get"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(
                url,
                json={"keys": keys},
                headers={"Authorization": f"Bearer {APP_TOKEN}"},
            )
        if resp.status_code != 200:
            return {}
        return {k: v for k, v in (resp.json().get("secrets") or {}).items() if v}
    except Exception:
        return {}


async def _call_oauth(endpoint: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    """POST to an oauth-v2 endpoint. Returns (status, body); (0, {}) if the app is unreachable."""
    if not ROUTER_URL or not APP_TOKEN:
        return 0, {}
    url = f"{ROUTER_URL}/api/services/v2/call/{OAUTH_SHORTNAME}/{endpoint}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload, headers={"Authorization": f"Bearer {APP_TOKEN}"})
        try:
            body = resp.json()
        except ValueError:
            body = {}
        return resp.status_code, body if isinstance(body, dict) else {}
    except Exception:
        return 0, {}


def github_action_url(body: Mapping[str, object]) -> str:
    """The URL a user must visit to unblock an oauth call, from a 401 or 403 body.

    A 403 needs a permission grant (`required_grant.grant_url`), a 401 needs the GitHub account to
    be connected at all (`authorize_url`). Both are browser flows only the user can complete.
    """
    authorize = body.get("authorize_url")
    if isinstance(authorize, str) and authorize:
        return authorize
    grant = body.get("required_grant")
    if isinstance(grant, dict):
        url = grant.get("grant_url")
        if isinstance(url, str) and url:
            return _with_return_to(url)
    return ""


def _with_return_to(url: str) -> str:
    """Fill in an empty `return_to`, which otherwise drops the user somewhere unhelpful.

    The provider ignores any value that doesn't start with "/", and the grant_url it builds leaves
    the parameter blank.
    """
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    if not any(k == "return_to" and v for k, v in query):
        query = [(k, v) for k, v in query if k != "return_to"] + [("return_to", "/")]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


async def fetch_github_accounts() -> list[str]:
    """GitHub logins that have an oauth grant, in order, deduped. [] if unavailable."""
    status, body = await _call_oauth("accounts", {"provider": "github", "scopes": GITHUB_SCOPES})
    if status != 200:
        return []
    raw = body.get("accounts")
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(a for a in raw if isinstance(a, str) and a))


async def mint_github_token() -> GithubToken | None:
    """Mint a `repo`-scoped GitHub token via the oauth-v2 app. None if unavailable.

    `account` is required in practice: it defaults to "default", which only resolves when exactly
    one account is connected and otherwise returns 401. So ask which accounts exist and name one,
    falling back to "default" when the listing itself is unavailable.
    """
    global _github_account
    account = _github_account
    if not account:
        accounts = await fetch_github_accounts()
        account = accounts[0] if accounts else "default"

    status, body = await _call_oauth(
        "token", {"provider": "github", "scopes": GITHUB_SCOPES, "account": account, "return_to": "/"}
    )
    if status != 200:
        # A stale cached account (revoked, renamed) would fail forever otherwise.
        _github_account = None
        return None
    token = str(body.get("access_token") or "").strip()
    if not token:
        return None
    _github_account = account
    return GithubToken(token=token, account=account, expires_at=_parse_expiry(body.get("expires_at")))


def _parse_expiry(raw: object) -> datetime | None:
    """Parse the ISO 8601 `expires_at`; None when absent, null or unparseable."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def fetch_github_token() -> str:
    """The access token alone, "" if unavailable — for callers that just need to clone."""
    minted = await mint_github_token()
    return minted.token if minted else ""


async def seed_oh_config() -> None:
    """Best-effort: write ~/.openhost/compute_space_cli.toml from env + secrets.

    Hostname comes from OPENHOST_ZONE_DOMAIN, which openhost injects into every app's env. The
    user-bound API token isn't auto-provided, so we fetch OH_TOKEN from secrets-v2. If the config
    file already exists (user configured manually), don't overwrite it.
    """
    cfg_path = HOME / ".openhost" / "compute_space_cli.toml"
    if cfg_path.exists():
        return
    hostname = os.environ.get("OPENHOST_ZONE_DOMAIN", "").strip()
    if not hostname:
        return
    token = (await fetch_secrets(["OH_TOKEN"])).get("OH_TOKEN", "").strip()
    if not token:
        return
    hostname = hostname.replace("https://", "").replace("http://", "").rstrip("/")
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        tomli_w.dumps(
            {
                "default_instance": hostname,
                "instances": {hostname: {"token": token}},
            }
        )
    )


def _write_gh_hosts(token: str, login: str) -> None:
    """Point the gh CLI at `token` by writing its hosts.yml directly.

    `gh auth login --with-token` is the documented path but it refuses this token: it insists on
    the `read:org` scope, which our `repo`-scoped grant doesn't include and doesn't need. Writing
    the file gh would have written skips that check. JSON scalars are valid YAML, so json.dumps
    handles the quoting.
    """
    GH_HOSTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    body = (
        "github.com:\n"
        f"    oauth_token: {json.dumps(token)}\n"
        f"    user: {json.dumps(login)}\n"
        '    git_protocol: "https"\n'
    )
    # Create it 0600 rather than writing then chmod-ing, so the token is never briefly world-readable.
    fd = os.open(GH_HOSTS_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(body)


def _git_env() -> dict[str, str]:
    """Environment that pins git's global config to GIT_CONFIG_PATH."""
    return {**os.environ, "GIT_CONFIG_GLOBAL": str(GIT_CONFIG_PATH)}


def _gh_user(token: str) -> dict[str, object] | None:
    """The GitHub account a token belongs to. None if the call fails.

    Doubles as a check that the token actually works, so a dud is never written into hosts.yml
    where it would leave gh reporting a broken login instead of no login.
    """
    proc = subprocess.run(["gh", "api", "user"], capture_output=True, env={**os.environ, "GH_TOKEN": token})
    if proc.returncode != 0:
        return None
    try:
        user = json.loads(proc.stdout)
    except ValueError:
        return None
    return user if isinstance(user, dict) and user.get("login") else None


def git_identity(user: Mapping[str, object]) -> tuple[str, str]:
    """The (name, email) to commit as, from a GitHub user object.

    Most accounts keep their email private, so the API returns null for it. GitHub's per-account
    noreply address is the right stand-in: it attributes the commit to the account without
    publishing an address the user chose not to publish.
    """
    login = str(user.get("login") or "")
    name = str(user.get("name") or "") or login
    email = str(user.get("email") or "")
    if not email and user.get("id") is not None:
        email = f"{user['id']}+{login}@users.noreply.github.com"
    return name, email


def _seed_git_identity(user: Mapping[str, object]) -> None:
    """Give git an identity, so committing works without being configured by hand.

    Without this `git commit` fails outright ("Author identity unknown"), which is a poor first
    thing to meet in a workbench that has just logged you in. Only fills in what is unset: a name
    or email the user chose stays put.
    """
    for key, value in zip(("user.name", "user.email"), git_identity(user), strict=True):
        if not value:
            continue
        existing = subprocess.run(["git", "config", "--global", "--get", key], capture_output=True, env=_git_env())
        if existing.returncode == 0 and existing.stdout.strip():
            continue
        subprocess.run(["git", "config", "--global", key, value], capture_output=True, env=_git_env())


def _gh_is_ours() -> bool:
    """Whether gh's stored credentials are ones we wrote.

    A hosts.yml without our marker belongs to the user — they ran `gh auth login` themselves, and
    overwriting it with a short-lived `repo`-scoped token would be a downgrade they didn't ask for.
    """
    return GH_MANAGED_MARKER.exists() or not GH_HOSTS_PATH.exists()


def _apply_gh_auth(minted: GithubToken) -> bool:
    """Log gh in with a freshly minted token. False if it didn't take."""
    user = _gh_user(minted.token)
    if user is None:
        return False
    login = str(user["login"])
    _write_gh_hosts(minted.token, login)
    GH_MANAGED_MARKER.parent.mkdir(parents=True, exist_ok=True)
    GH_MANAGED_MARKER.write_text(login)
    # Route git's own auth through gh, so `git push` in a terminal works without the user pasting a
    # token into a remote URL. gh re-reads hosts.yml per call, so refreshes flow through for free.
    subprocess.run(["gh", "auth", "setup-git"], capture_output=True, env=_git_env())
    _seed_git_identity(user)
    return True


async def _refresh_gh_auth() -> float:
    """Mint a token and hand it to gh. Returns how long to wait before doing it again."""
    loop = asyncio.get_running_loop()
    if not await loop.run_in_executor(None, _gh_is_ours):
        return GH_REFRESH_MAX_SECONDS
    minted = await mint_github_token()
    if not minted:
        return GH_REFRESH_MAX_SECONDS
    if not await loop.run_in_executor(None, _apply_gh_auth, minted):
        return GH_REFRESH_MAX_SECONDS
    return minted.seconds_until_refresh()


async def seed_gh_auth() -> float:
    """Log the gh CLI in from the oauth grant. Best-effort; returns the next refresh delay.

    Awaited at startup so the first terminal already has a working gh.
    """
    try:
        return await _refresh_gh_auth()
    except Exception:
        return GH_REFRESH_MAX_SECONDS


async def refresh_gh_auth_periodically(delay: float) -> None:
    """Re-mint gh's token before it expires, for the life of the process.

    Tokens are short-lived, so without this a terminal left open overnight would find gh logged
    out. Refreshing centrally means every terminal — including ones already running — picks the new
    token up, which putting it in each shell's environment would not achieve.
    """
    while True:
        await asyncio.sleep(delay)
        try:
            delay = await _refresh_gh_auth()
        except asyncio.CancelledError:
            raise
        except Exception:
            delay = GH_REFRESH_MAX_SECONDS


async def get_anthropic_key() -> str:
    """Return the cached ANTHROPIC_API_KEY, fetching from secrets on first call."""
    global _anthropic_key
    if _anthropic_key:
        return _anthropic_key
    async with _anthropic_lock:
        if _anthropic_key:
            return _anthropic_key
        key = (await fetch_secrets(["ANTHROPIC_API_KEY"])).get("ANTHROPIC_API_KEY", "")
        if key:
            _anthropic_key = key
        return key
