# claude-workbench

An openhost app that gives you in-browser terminals, preinstalled Claude Code, and a place to keep several independent copies of a repo side by side. Meant as a starting point for building or debugging openhost apps.

You work in **projects** and **workspaces** — see [Projects and workspaces](#projects-and-workspaces) below, which is the shape of the whole UI.

## What's inside the container

- `@anthropic-ai/claude-code` (npm, installed at image build time). It runs in whichever workspace you opened it from. By default, `claude` is aliased with `--dangerously-skip-permissions` in this sandbox.
- Python 3 + git + the usual tools.
- A clone of `https://github.com/imbue-openhost/openhost` placed at `~/openhost` on first container start (override with `OPENHOST_REPO_URL` or `OPENHOST_DIR` env vars).
- A Claude Code skill at `~/.claude/skills/openhost/` that points Claude at the curated docs in the local openhost clone.
- Global Claude instructions at `~/.claude/CLAUDE.md`, which Claude reads in every workspace. It's a symlink to `claude-home/CLAUDE.md` in this repo, so app updates ship new text automatically. Your own additions go in `~/.claude/CLAUDE.local.md`, which the bundled file imports and the entrypoint never overwrites (an existing `~/.claude/CLAUDE.md` is moved there on first start rather than replaced).
- A checkout of this repo at `~/claude-code-container`, so you can work on the workbench from inside the workbench (override with `WORKBENCH_REPO_URL` or `WORKBENCH_DIR`). See the warning below before editing it.

### Editing the workbench from inside itself

> **Local edits in `~/claude-code-container` are not durable.** If you're reading this file there, that includes this one.

The checkout tracks the repo's default branch, so it can be *ahead of* the image you're actually running — it's a convenience for hacking on the workbench, not a record of what got built.

When the app is updated from outside (a rebuild via the dashboard, `oh app reload --update`, or a redeploy), the entrypoint resets that checkout to the remote with `git reset --hard` and `git clean -fd`, discarding anything uncommitted. An ordinary container restart does *not* do this — the entrypoint compares `/app/.image-stamp`, which only changes when the image is rebuilt with new content, so restarts leave your work alone.

Push anything you want to keep, and treat the checkout as disposable. The resync is deliberately best-effort and never fails startup: if GitHub is unreachable or git is unhappy, it logs a warning, leaves the directory as it is, and the workbench boots anyway.

Authentication for `claude` is whatever the user sets up inside the terminal — either `ANTHROPIC_API_KEY` in the environment or an interactive `claude login`. The workbench doesn't manage that.

`HOME` lives on the app's persistent data dir (`/data/app_data/claude-workbench/home`), so a `claude login`, the openhost clone, and shell history all survive container redeploys. The workbench's own prompt, aliases, and PATH fixups live at `/etc/profile.d/workbench.sh` (sourced by both login and non-login interactive bash), so `~/.bashrc` and `~/.bash_profile` are entirely yours — anything you write there sticks around and is never overwritten by image updates.

As a convenience, if the `secrets-v2` app is installed and `ANTHROPIC_API_KEY` is set there, the workbench fetches it on first PTY launch and exports it into every new terminal's environment. This is best-effort — if the secrets app isn't around the terminal still works, you just have to set the key yourself.

## GitHub auth (`gh`, pushing, private repos)

The workbench can mint a GitHub token through openhost's `oauth-v2` app, which is what lets it clone private repos and push. On startup `seed_gh_auth()` tries to log `gh` in automatically; when that hasn't happened, here is the manual flow and the two things that reliably trip people up.

**1. Mint a token.** `account` is required and must match a GitHub login that has already been granted — it defaults to `"default"`, which matches nothing, so leaving it out returns `permission_required` even when a valid grant exists:

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

If that returns `permission_required`, the response carries a `grant_url`. Open it in a browser and approve. Give `return_to` a value starting with `/` (a bare `/` is fine) — the provider ignores anything that doesn't, so an empty one just drops you somewhere unhelpful. You can confirm what is granted with:

```bash
oh curl -- -s "https://$OPENHOST_ZONE_DOMAIN/api/permissions/v2?app_id=$OPENHOST_APP_ID"
```

**2. Use the token via `GH_TOKEN`, not `gh auth login`.** The minted token is `repo`-scoped, and `gh auth login --with-token` rejects it with *"missing required scope 'read:org'"*. Export it instead:

```bash
export GH_TOKEN=<token>
gh api user -q .login          # works
git push "https://x-access-token:$GH_TOKEN@github.com/<owner>/<repo>.git" <branch>
```

Tokens are short-lived; re-mint when one stops working. The `/github-auth` skill walks Claude through all of this.

> **Known gap:** `fetch_github_token()` in `remote_services.py` requests a token without an `account`, so it always gets `permission_required` once grants are tied to a real login — meaning `seed_gh_auth()` silently does nothing and `gh` is left logged out. Both failures are swallowed by design (they're best-effort), so the only symptom is `gh` not being authenticated.

## Projects and workspaces

A **project** is a git repo you've told the workbench about: a name, a clone URL, and optionally a setup command. A **workspace** is one full copy of that repo on disk. There can be as many workspaces of a project as you like and none of them is special — there is deliberately no canonical checkout to be careful around. They're independent working copies, similar in spirit to git worktrees but without the shared `.git` (so a broken workspace can only break itself, and you can delete one without a thought).

```
~/.workbench/projects.json            the project list
~/.workbench/mirrors/<project>.git    one bare mirror per project
~/workspaces/<project>/<workspace>/   the workspaces themselves
```

The mirror is what makes a second workspace cheap: the first one pays for a network clone into the mirror, and every workspace after that is a local clone from it (hardlinked objects, so near-instant and nearly free on disk). Each workspace's `origin` is rewritten to the real remote afterwards, so `git push`, `git pull` and anything reading the remote behave exactly as in an ordinary checkout.

Workspaces are read back off disk rather than tracked in a file of their own, so the sidebar can't drift out of sync with what's actually there. All of it lives under `$HOME`, which openhost points at the app's persistent data dir, so projects and workspaces survive redeploys.

### Creating one

Add a project with **+** in the sidebar (a clone URL is the only thing required); the workbench checks the repo is reachable before saving it, so a typo fails once, there and then, rather than in every workspace afterwards. Then **+** next to a project creates a workspace in it: pick a name, and optionally a branch, tag or commit — blank means the default branch. A workspace does not create a branch for you.

Creating one opens a terminal running the bootstrap: update the mirror, clone, run the project's setup command if it has one, then hand the terminal over to Claude Code. You watch all of it happen; if a step fails you're left in a shell in the workspace rather than staring at a tab that vanished.

Deleting a workspace deletes the directory and kills its terminals, and is not recoverable. Removing a project forgets it and deletes its mirror, but refuses while it still has workspaces — those hold your work.

### The API behind it

```
GET    /api/projects                          projects, each with its workspaces
POST   /api/projects        {repo_url, name?, setup?}
PATCH  /api/projects/{id}   {name?, setup?}
DELETE /api/projects/{id}
POST   /api/workspaces      {project_id, name?, ref?}
DELETE /api/workspaces/{project}/{workspace}
```

## The UI

`GET /` serves a [Solid](https://www.solidjs.com/) app: the project/workspace sidebar on the left, and the workspace you're in filling the rest.

Terminals are laid out with [dockview](https://dockview.dev/) — drag a tab to split the area, drop it back to merge, and the arrangement is remembered per workspace. Each terminal opens its own WebSocket to `/terminal/ws`, which bridges to a PTY running `bash -l` inside the container.

Terminals belong to a workspace. You only see the ones for the workspace you're in; the others keep running in the background, and switching back reattaches to them with their scrollback intact. Opening a workspace with no terminals starts one running Claude Code in it.

Closing a panel kills the terminal behind it. If you close the browser instead (or a restart brings terminals back), **+ terminal** in the top bar lists any that are running without a panel so you can reattach.

The frontend source is in `ui/`; see [Development](#development) for the build.

### Colour schemes

A picker in the top-right of the tab bar switches between **Dark** (the default), **Solarized Light** and **Solarized Dark**. It applies immediately — open terminals are recoloured in place, no reload — and is saved server-side in `$HOME/.workbench/ui.json`, so it persists across restarts and rebuilds and follows you to any browser.

```
POST /api/ui/settings   { "theme": "solarized-light" }
```

A scheme is defined in two halves, because the terminal and the chrome are painted by different machinery:

- `src/server/static/themes.css` — CSS variables selected by `data-theme` on `<html>`, covering the sidebar, dialogs, dockview's tab strip and the side panel. The bare `:root` block is the dark default, so an unknown or absent theme falls back to the original look. It's linked rather than bundled, because it has to apply before the app renders and the side panel's own page shares it.
- `ui/src/themes.js` — the terminal's 16-colour ANSI palette, which xterm.js needs as a JS object since it renders to a canvas.

dockview's own chrome needs neither: `ui/src/styles/dockview.css` maps its CSS variables onto the same palette, so the layout follows the picker for free.

The server renders `data-theme` into the page, so there's no flash of the wrong colours before the app boots. Adding a scheme means touching `THEMES` in `ui_settings.py`, `themes.css`, and `ui/src/themes.js` — a test asserts those three lists agree, so a half-added theme fails rather than rendering unstyled.

### Side-by-side panel (opt-in)

A resizable pane beside the terminal that loads any URL in an iframe — handy for watching a dev server or another openhost app while you work. It's off by default. The easiest way to turn it on is the bundled skill: run `/side-by-side` in Claude Code and ask for it on or off. Under the hood that's just:

```
POST /api/ui/settings   { "side_panel": true }
GET  /api/ui/settings   -> { "side_panel": false }
```

The setting is stored in `$HOME/.workbench/ui.json`. Since openhost points `HOME` at the app's persistent data dir, the choice survives container rebuilds. It takes effect on the next page load; reloading is safe because terminals live server-side and the page re-attaches to the running session.

Drag the divider to resize, or double-click it to reset; it's focusable, with arrow keys (Shift for larger steps). The pane's toolbar has a URL bar plus reload, open-in-a-real-tab, and hide buttons, and **◻ panel** in the top bar brings a hidden pane back. Width, visibility and last URL are remembered per browser in `localStorage`; the on/off setting above is server-side.

The setting reaches the page as `window.__WORKBENCH__.side_panel`, so the panel is only mounted when it's on. It takes effect on the next page load.

## The `open-workspace` service

claude-workbench is the first **provider** of the `open-workspace` openhost service: *"here is a repo at a commit — send me to a place where a person can work on it."* The contract is defined in this repo under [`services/open-workspace/`](services/open-workspace/) and is implementation-neutral, so a future provider (a cloud IDE, Cursor, PyCharm…) can satisfy it without any caller changing.

```
POST /open-workspace          (form or JSON body, or query params)
GET  /open-workspace          (query params)
  repo=<clone-url>&ref=<commit|tag|branch>
  -> 303 redirect to /?session=<token>
```

GET is accepted in addition to the canonical POST as a workaround for the openhost router's login bounce: an unauthenticated POST gets `302`'d to `/login?next=…`, and a browser following that demotes the eventual return hop to GET (only HTTP `307`/`308` preserve method). Accepting GET means the post-login landing still resolves instead of `405`-ing. Once the router switches to method-preserving redirects this can go away.

- `repo` (required) — an `https://`, `http://`, `ssh://`, or `git@…` clone URL. Other transports (e.g. `ext::`, `file://`) are rejected.
- `ref` (required) — a commit, tag, or branch identifying the exact code.

The endpoint registers `repo` as a project if it isn't one already, creates a workspace at `ref`, and 303-redirects you into a terminal sitting in it. Status codes follow the contract: `400` for a missing/malformed `repo` or `ref`, `404` when the repo or a named ref doesn't exist, `403` when the repo is private and the workbench has no authorization to reach it, and `5xx` for internal errors. The workspace URL is delivered in the redirect `Location`, never in a response body.

Every call gets its **own** workspace (`main`, `main-2`, …) rather than reusing one, so a second visit can never disturb work in progress from the first. That's affordable precisely because workspaces clone from the project's local mirror.

### Private repos

To open a private repo the workbench mints a short-lived, `repo`-scoped GitHub token via the openhost `oauth` service — the same flow openhost itself uses to clone private repos — injects it into the clone/fetch URL transiently, and strips it from the remote afterward so the token is never persisted on disk. Public repos clone without a token, and if no GitHub grant is available the clone falls back to an unauthenticated attempt.

## Development

```bash
just setup             # deps, pre-commit hooks, playwright chromium, and the UI
just run               # build the UI and run locally on http://localhost:8080
just build-ui          # build the frontend into src/server/static/ui
just watch-ui          # rebuild the frontend on every change
just test              # fast unit tests
just test-integration  # build the image and exercise it under podman
just check             # lint, format, typecheck
just build             # build the container image
```

Python work uses [uv](https://docs.astral.sh/uv/). Use `uv add <pkg>` to add a dependency and `uv add --group dev <pkg>` for a dev-only one.

The backend lives in `src/server/`: `routes/` holds the HTTP handlers, `projects/` the project/workspace model and the `create_workspace.sh` it launches, and `tabs.py` the PTY plumbing. `entrypoint.sh`, `workbench.sh`, `skills/` and `claude-home/` sit at the repo root because the container refers to them by absolute path.

The frontend is a Solid app in `ui/`, built by vite into `src/server/static/ui/bundle.{js,css}` — fixed names, since the page is a Jinja template that references them. That directory is generated and not committed; `just build-ui` makes it locally and the Dockerfile's node stage makes it for the image, so nothing needs a built bundle checked in. While working on the UI, run `just watch-ui` beside the server: the files land where it already serves them, so a reload is enough (and reloading is safe — terminals live server-side and the page reattaches).

`just test-integration` uses the OpenHost test harness (the `openhost[test-harness]` package), which builds the Dockerfile and runs the app under **podman** fronted by the real OpenHost router, so podman must be running. `stack.url` requires owner auth (use `stack.owner_session` for requests, or `stack.playwright_login(page)` for browser tests); `stack.app_url` hits the container directly. See `tests/conftest.py` for the `stack` fixture.
