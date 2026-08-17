import asyncio
import os
import subprocess

import httpx
import tomli_w

from server.config import HOME

ROUTER_URL = os.environ.get("OPENHOST_ROUTER_URL", "")
APP_TOKEN = os.environ.get("OPENHOST_APP_TOKEN", "")
SECRETS_SHORTNAME = "secrets"
OAUTH_SHORTNAME = "oauth"

_anthropic_key: str | None = None
_anthropic_lock = asyncio.Lock()


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


async def fetch_github_token() -> str:
    """Mint a `repo`-scoped GitHub token via the oauth-v2 app. "" if unavailable.

    Mirrors openhost's own clone flow (core/oauth.py `get_oauth_token`): the token lets us clone
    private repos openhost has access to. Best-effort — if the oauth app isn't installed, the grant
    is missing, or no GitHub account is connected, we get a non-200 and return "" so the caller
    falls back to an unauthenticated clone.
    """
    if not ROUTER_URL or not APP_TOKEN:
        return ""
    url = f"{ROUTER_URL}/api/services/v2/call/{OAUTH_SHORTNAME}/token"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                json={"provider": "github", "scopes": ["repo"]},
                headers={"Authorization": f"Bearer {APP_TOKEN}"},
            )
        if resp.status_code != 200:
            return ""
        return (resp.json().get("access_token") or "").strip()
    except Exception:
        return ""


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


async def seed_gh_auth() -> None:
    """Authenticate gh CLI with the GitHub OAuth token from the oauth provider. Best-effort."""
    loop = asyncio.get_running_loop()
    try:
        already = await loop.run_in_executor(
            None,
            lambda: (
                subprocess.run(
                    ["gh", "auth", "status"],
                    capture_output=True,
                ).returncode
                == 0
            ),
        )
        if already:
            return

        token = await fetch_github_token()
        if not token:
            return

        await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                ["gh", "auth", "login", "--with-token"],
                input=token.encode(),
                capture_output=True,
            ),
        )
    except Exception:
        pass


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
