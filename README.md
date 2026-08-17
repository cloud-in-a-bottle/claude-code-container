# claude-workbench

An openhost app that gives you tabbed in-browser terminals, preinstalled
Claude Code, and a cloned copy of the openhost repo. Meant as a starting
point for building or debugging openhost apps.

## What's inside the container

- `@anthropic-ai/claude-code` (npm, installed at image build time). On
  startup, it runs in `~/my_project`. By default, `claude` is aliased with
  `--dangerously-skip-permissions` in this sandbox. 
- Python 3 + git + the usual tools.
- `chisel` + `openssh-server`, wired up so you can SSH into the workbench from
  outside over the same HTTPS subdomain (see [SSH access](#ssh-access)).
- A clone of `https://github.com/imbue-openhost/openhost` placed at
  `~/openhost` on first container start (override with `OPENHOST_REPO_URL`
  or `OPENHOST_DIR` env vars).
- A Claude Code skill at `~/.claude/skills/openhost/` that points Claude
  at the curated docs in the local openhost clone.
- A checkout of this repo at `~/claude-code-container`, so you can work on
  the workbench from inside the workbench (override with `WORKBENCH_REPO_URL`
  or `WORKBENCH_DIR`). See the warning below before editing it.

### Editing the workbench from inside itself

> **Local edits in `~/claude-code-container` are not durable.** If you're
> reading this file there, that includes this one.

The checkout tracks the repo's default branch, so it can be *ahead of* the
image you're actually running — it's a convenience for hacking on the
workbench, not a record of what got built.

When the app is updated from outside (a rebuild via the dashboard, `oh app
reload --update`, or a redeploy), the entrypoint resets that checkout to the
remote with `git reset --hard` and `git clean -fd`, discarding anything
uncommitted. An ordinary container restart does *not* do this — the entrypoint
compares `/app/.image-stamp`, which only changes when the image is rebuilt with
new content, so restarts leave your work alone.

Push anything you want to keep, and treat the checkout as disposable. The
resync is deliberately best-effort and never fails startup: if GitHub is
unreachable or git is unhappy, it logs a warning, leaves the directory as it
is, and the workbench boots anyway.

Authentication for `claude` is whatever the user sets up inside the
terminal — either `ANTHROPIC_API_KEY` in the environment or an interactive
`claude login`. The workbench doesn't manage that.

`HOME` lives on the app's persistent data dir
(`/data/app_data/claude-workbench/home`), so a `claude login`, the openhost
clone, and shell history all survive container redeploys. The workbench's
own prompt, aliases, and PATH fixups live at `/etc/profile.d/workbench.sh`
(sourced by both login and non-login interactive bash), so `~/.bashrc` and
`~/.bash_profile` are entirely yours — anything you write there sticks
around and is never overwritten by image updates.

As a convenience, if the `secrets-v2` app is installed and `ANTHROPIC_API_KEY`
is set there, the workbench fetches it on first PTY launch and exports it
into every new terminal's environment. This is best-effort — if the secrets
app isn't around the terminal still works, you just have to set the key
yourself.

## GitHub auth (`gh`, pushing, private repos)

The workbench can mint a GitHub token through openhost's `oauth-v2` app, which
is what lets it clone private repos and push. On startup `seed_gh_auth()` tries
to log `gh` in automatically; when that hasn't happened, here is the manual
flow and the two things that reliably trip people up.

**1. Mint a token.** `account` is required and must match a GitHub login that
has already been granted — it defaults to `"default"`, which matches nothing, so
leaving it out returns `permission_required` even when a valid grant exists:

```bash
# list the accounts that have grants (may repeat a name; dedupe it)
curl -s -X POST "$OPENHOST_ROUTER_URL/api/services/v2/call/oauth/accounts" \
  -H "Authorization: Bearer $OPENHOST_APP_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"provider":"github","scopes":["repo"]}'

curl -s -X POST "$OPENHOST_ROUTER_URL/api/services/v2/call/oauth/token" \
  -H "Authorization: Bearer $OPENHOST_APP_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"provider":"github","scopes":["repo"],"account":"<login>"}'
```

If that returns `permission_required`, the response carries a `grant_url`. Open
it in a browser and approve. Give `return_to` a value starting with `/` (a bare
`/` is fine) — the provider ignores anything that doesn't, so an empty one just
drops you somewhere unhelpful. You can confirm what is granted with:

```bash
oh curl -- -s "https://$OPENHOST_ZONE_DOMAIN/api/permissions/v2?app_id=$OPENHOST_APP_ID"
```

**2. Use the token via `GH_TOKEN`, not `gh auth login`.** The minted token is
`repo`-scoped, and `gh auth login --with-token` rejects it with *"missing
required scope 'read:org'"*. Export it instead:

```bash
export GH_TOKEN=<token>
gh api user -q .login          # works
git push "https://x-access-token:$GH_TOKEN@github.com/<owner>/<repo>.git" <branch>
```

Tokens are short-lived; re-mint when one stops working. The `/github-auth`
skill walks Claude through all of this.

> **Known gap:** `fetch_github_token()` in `remote_services.py` requests a token
> without an `account`, so it always gets `permission_required` once grants are
> tied to a real login — meaning `seed_gh_auth()` silently does nothing and `gh`
> is left logged out. Both failures are swallowed by design (they're
> best-effort), so the only symptom is `gh` not being authenticated.

## The UI

`GET /` serves a tabbed xterm.js page. Each tab opens its own WebSocket
to `/terminal/ws`, which bridges to a PTY running `bash -l` inside the
container.

### Colour schemes

A picker in the top-right of the tab bar switches between **Dark** (the
default), **Solarized Light** and **Solarized Dark**. It applies immediately —
open terminals are recoloured in place, no reload — and is saved server-side in
`$HOME/.workbench/ui.json`, so it persists across restarts and rebuilds and
follows you to any browser.

```
POST /api/ui/settings   { "theme": "solarized-light" }
```

A scheme is defined in two halves, because the terminal and the chrome are
painted by different machinery:

- `static/themes.css` — CSS variables selected by `data-theme` on `<html>`,
  covering the tab bar, menus, and side panel. The bare `:root` block is the
  dark default, so an unknown or absent theme falls back to the original look.
- `static/theme.js` — the terminal's 16-colour ANSI palette, which xterm.js
  needs as a JS object since it renders to a canvas.

The server renders `data-theme` into the page, so there's no flash of the wrong
colours before the picker initialises. Adding a scheme means touching
`THEMES` in `ui_settings.py`, `themes.css`, and `theme.js` — a test asserts
those three lists agree, so a half-added theme fails rather than rendering
unstyled.

### Side-by-side panel (opt-in)

A resizable pane beside the terminal that loads any URL in an iframe —
handy for watching a dev server or another openhost app while you work.
It's off by default. The easiest way to turn it on is the bundled skill:
run `/side-by-side` in Claude Code and ask for it on or off. Under the
hood that's just:

```
POST /api/ui/settings   { "side_panel": true }
GET  /api/ui/settings   -> { "side_panel": false }
```

The setting is stored in `$HOME/.workbench/ui.json`. Since openhost points
`HOME` at the app's persistent data dir, the choice survives container
rebuilds. It takes effect on the next page load; reloading is safe because
terminals live server-side and the page re-attaches to the running session.

Drag the divider to resize, or double-click it to reset; it's focusable, with
arrow keys (Shift for larger steps). The pane's toolbar has a URL bar plus
reload, open-in-a-real-tab, and hide buttons, and **◻ panel** in the tab bar
brings a hidden pane back. Width, visibility and last URL are remembered per
browser in `localStorage`; the on/off setting above is server-side.

When enabled, `index.html` pulls in `static/side-panel.js`, which injects
its own CSS and builds its own DOM. Nothing is fetched when it's off. It
resizes the terminal by dispatching a `resize` event rather than calling
into `app.js`, since `app.js` already refits xterm on that event.

## Prefilling a Claude session (preview)

There's a stub for the eventual "open a Claude session with this context"
flow:

```
POST /api/sessions
  { "prompt": "fix this 503", "context": "<app logs / request info>" }
  -> { "id": "<token>", "url": "/?session=<token>" }
```

Opening the returned URL launches a new tab that runs `claude` and pipes
the combined context+prompt into its stdin. The session id is consumed on
first use.

This is intentionally minimal — the intent is that openhost error pages
(503 from openhost or from an app) can POST request info and app logs
here and then link the user to a pre-loaded Claude session.

## The `open-workspace` service

claude-workbench is the first **provider** of the `open-workspace` openhost
service: *"here is a repo at a commit — send me to a place where a person can
work on it."* The contract is defined in this repo under
[`services/open-workspace/`](services/open-workspace/) and is
implementation-neutral, so a future provider (a cloud IDE, Cursor, PyCharm…)
can satisfy it without any caller changing.

```
POST /open-workspace          (form or JSON body, or query params)
GET  /open-workspace          (query params)
  repo=<clone-url>&ref=<commit|tag|branch>
  -> 303 redirect to /?session=<token>
```

GET is accepted in addition to the canonical POST as a workaround for the
openhost router's login bounce: an unauthenticated POST gets `302`'d to
`/login?next=…`, and a browser following that demotes the eventual return
hop to GET (only HTTP `307`/`308` preserve method). Accepting GET means the
post-login landing still resolves instead of `405`-ing. Once the router
switches to method-preserving redirects this can go away.

- `repo` (required) — an `https://`, `http://`, `ssh://`, or `git@…` clone
  URL. Other transports (e.g. `ext::`, `file://`) are rejected.
- `ref` (required) — a commit, tag, or branch identifying the exact code.

The endpoint clones `repo` at `ref` and 303-redirects you into a terminal
sitting in that checkout. Status codes follow the contract: `400` for a
missing/malformed `repo` or `ref`, `404` when the repo or a named ref doesn't
exist, `403` when the repo is private and the workbench has no authorization
to reach it, and `5xx` for internal errors. The workspace URL is delivered in
the redirect `Location`, never in a response body.

The clone lands at `$HOME/<repo-name>`. Opening the same repo again **reuses**
that directory rather than clobbering it: it fetches, and if the working tree
has uncommitted changes it asks — right in the terminal — whether to commit
them to a `workbench-wip-…` branch, stash them, drop them, or keep them as-is
and stop. Only once the tree is clean does it check out the requested ref. (If
the tab is closed/stale and there's no one to answer, it leaves your changes
untouched and gives you a shell.)

### Private repos

To open a private repo the workbench mints a short-lived, `repo`-scoped GitHub
token via the openhost `oauth` service — the same flow openhost itself uses to
clone private repos — injects it into the clone/fetch URL transiently, and
strips it from the remote afterward so the token is never persisted on disk.
Public repos clone without a token, and if no GitHub grant is available the
clone falls back to an unauthenticated attempt.

## SSH access

You can SSH into the workbench from your own machine — a real terminal, `scp`,
`rsync`, `git` over SSH, editor remote-dev, port-forwarding — without opening any
firewall ports. Traffic is tunneled with [chisel](https://github.com/jpillora/chisel)
over the workbench's existing HTTPS subdomain.

### How it's wired

`chisel server` runs as the container's front-door on the app port. It reverse-proxies
all normal traffic to the Quart terminal UI (moved to a loopback backend port), so the
browser experience is unchanged, and it accepts chisel tunnel connections on the public
`/_chisel` path. Inside the container, `sshd` listens on `127.0.0.1:22` only — it is
never bound to a host port; the sole way in is the tunnel.

```
  your machine                     openhost (HTTPS)              workbench container
  ┌────────────┐   chisel over    ┌──────────────┐   proxy     ┌──────────────────┐
  │ ssh client │──────wss────────▶│  router :443  │────────────▶│ chisel :$PORT     │
  │ chisel cli │   /_chisel path  └──────────────┘             │   ├─ /_chisel→ssh │
  └────────────┘                                               │   └─ else → quart │
        └── localhost:2222 ──────────────(tunnel)──────────────▶│ sshd 127.0.0.1:22 │
                                                                └──────────────────┘
```

Two layers of auth guard the tunnel (the openhost session cookie does **not** — the
external client has no browser login, which is why `/_chisel` is a `public_path`):

1. **chisel `--auth`** — a `user:pass` credential. Auto-generated and persisted on first
   boot, or set the `CHISEL_AUTH` secret to pin your own.
2. **SSH key auth** — `sshd` is key-only (`PermitRootLogin prohibit-password`,
   `PasswordAuthentication no`).

### One-time setup

1. **Add your local machine's SSH public key to the workbench.** Either paste it into
   a workbench terminal tab (persists across redeploys, since `$HOME` is on the app's
   data dir):

   ```
   echo 'ssh-ed25519 AAAA... you@host' >> ~/.ssh/authorized_keys
   ```

   …or set the `SSH_AUTHORIZED_KEYS` secret in the secrets app (one or more keys,
   newline-separated) and restart the workbench.

2. **Grab the chisel credential.** Open a terminal tab — the login banner prints it,
   along with a ready-to-paste connect command. (Or set `CHISEL_AUTH` yourself.)

3. **Install chisel on your machine** — see the
   [chisel releases](https://github.com/jpillora/chisel/releases).

### Connecting

The terminal banner prints a ready-to-paste command with the real generated credential inlined.
By hand, it looks like this — open the tunnel, then SSH through it:

```bash
chisel client --auth 'workbench:<token-from-banner>' \
  https://claude-workbench.<zone>/_chisel 2222:localhost:22 &
ssh -p 2222 root@localhost
```

Or, using the bundled helper ([`scripts/ssh-connect.sh`](scripts/ssh-connect.sh)):

```bash
CHISEL_AUTH='workbench:<token-from-banner>' ./scripts/ssh-connect.sh https://claude-workbench.<zone>
```

The SSH session lands in the same persistent `$HOME` as the browser terminals (the
openhost clone, `my_project`, shell history). The `sshd` host key is persisted too, so
you won't get host-key-changed warnings across redeploys.

To rotate the generated chisel credential, run this inside a workbench terminal:

```bash
/app/scripts/rotate-chisel-auth.sh
```

The script writes a new `~/.ssh/chisel-auth` and restarts the workbench. If you set a
`CHISEL_AUTH` secret, rotate or remove that secret instead — it overrides the generated file on
every start.

## Running locally without openhost

```
pip install quart hypercorn
python3 server.py
```

Then open http://localhost:8080. This runs the terminal UI directly, without the
chisel front-door or `sshd` (those are started by `tunnel.sh`, the container
entrypoint's handoff). To exercise the full tunnel path locally, build and run the
image (`just serve`) so `chisel` fronts the app.
