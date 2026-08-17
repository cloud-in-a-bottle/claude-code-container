# claude-workbench

An openhost app that gives you tabbed in-browser terminals, preinstalled
Claude Code, and a cloned copy of the openhost repo. Meant as a starting
point for building or debugging openhost apps.

## What's inside the container

- `@anthropic-ai/claude-code` (npm, installed at image build time). On
  startup, it runs in `~/my_project`. By default, `claude` is aliased with
  `--dangerously-skip-permissions` in this sandbox. 
- Python 3 + git + the usual tools.
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

## Running locally without openhost

```
pip install quart hypercorn
python3 server.py
```

Then open http://localhost:8080.
