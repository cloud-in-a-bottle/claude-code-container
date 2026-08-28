# claude-workbench

An openhost app that gives you in-browser terminals, preinstalled Claude Code, and a place to keep several independent copies of a repo side by side. Meant as a starting point for building or debugging openhost apps.

You work in **projects** and **workspaces** — see [Projects and workspaces](#projects-and-workspaces) below, which is the shape of the whole UI.

## What's inside the container

- `@anthropic-ai/claude-code` (npm, installed at image build time). It runs in whichever workspace you opened it from. By default, `claude` is aliased with `--dangerously-skip-permissions` in this sandbox, and the folder-trust dialog is skipped so a new workspace opens straight into the conversation.
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

The workbench mints a GitHub token through openhost's `oauth-v2` app, and logs `gh` in with it on startup. Every terminal is authenticated — including ones already open — so `gh`, `git push` and private clones just work, with no token in any shell's environment.

`seed_gh_auth()` runs at startup and `refresh_gh_auth_periodically()` re-mints before the token expires, since these tokens are short-lived and a terminal left open overnight would otherwise find `gh` logged out.

Two things about the flow are non-obvious, and both used to break it:

- **`account` is required.** It defaults to `"default"`, which only resolves when exactly one account is connected and otherwise returns `401`. So `mint_github_token()` calls `/accounts` first and names a login explicitly, falling back to `"default"` only when that listing is unavailable.
- **`gh auth login --with-token` rejects the token**, insisting on `read:org`, which the `repo`-scoped grant doesn't include and doesn't need. So `_write_gh_hosts()` writes `~/.config/gh/hosts.yml` directly — the file `gh` would have written — and `gh auth setup-git` points git's credential helper at it.

It also gives git an identity from the same account — `user.name` and `user.email` in `~/.gitconfig` — because without one `git commit` fails outright with *"Author identity unknown"*, which is a poor first thing to meet in a workbench that has just logged you in. Most accounts keep their email private, so it falls back to the account's `ID+login@users.noreply.github.com` address: that attributes the commit without publishing an address you chose not to publish.

Nothing you set yourself is clobbered. `hosts.yml` is only rewritten when `~/.workbench/gh-auth-managed` marks it as the workbench's own, and the identity only fills in whichever of `user.name` / `user.email` is unset.

### When it hasn't happened

If `gh auth status` says logged out, the grant is usually missing. Ask for one:

```bash
# the accounts that have grants (may repeat a name; dedupe it)
curl -s -X POST "$OPENHOST_ROUTER_URL/api/services/v2/call/oauth/accounts" \
  -H "Authorization: Bearer $OPENHOST_APP_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"provider":"github","scopes":["repo"]}'

curl -s -X POST "$OPENHOST_ROUTER_URL/api/services/v2/call/oauth/token" \
  -H "Authorization: Bearer $OPENHOST_APP_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"provider":"github","scopes":["repo"],"account":"<login>","return_to":"/"}'
```

A `403 permission_required` carries a `grant_url`; a `401 authorization_required` carries an `authorize_url`. Both are browser flows only the user can complete — open the URL and approve, then restart the app (or wait for the next refresh). Give `return_to` a value starting with `/`; the provider ignores anything else, so a blank one drops you somewhere unhelpful.

Confirm what is granted with:

```bash
oh curl -- -s "https://$OPENHOST_ZONE_DOMAIN/api/permissions/v2?app_id=$OPENHOST_APP_ID"
```

To use a token by hand, export it rather than running `gh auth login`:

```bash
export GH_TOKEN=<token>
gh api user -q .login
```

The `/github-auth` skill walks Claude through all of this.

## Projects and workspaces

A **project** is a git repo you've told the workbench about: a name, a clone URL, and optionally a default branch and a setup command. A **workspace** is one full copy of that repo on disk. There can be as many workspaces of a project as you like and none of them is special — there is deliberately no canonical checkout to be careful around. They're independent working copies, similar in spirit to git worktrees but without the shared `.git` (so a broken workspace can only break itself, and you can delete one without a thought).

```
~/.workbench/projects.json            the project list
~/.workbench/mirrors/<project>.git    one bare mirror per project
~/workspaces/<project>/<workspace>/   the workspaces themselves
```

The mirror is what makes a second workspace cheap: the first one pays for a network clone into the mirror, and every workspace after that is a local clone from it (hardlinked objects, so near-instant and nearly free on disk). Each workspace's `origin` is rewritten to the real remote afterwards, so `git push`, `git pull` and anything reading the remote behave exactly as in an ordinary checkout.

Workspaces are read back off disk rather than tracked in a file of their own, so the sidebar can't drift out of sync with what's actually there. All of it lives under `$HOME`, which openhost points at the app's persistent data dir, so projects and workspaces survive redeploys.

### Creating one

Add a project with **+** in the sidebar (a clone URL is the only thing required); the workbench checks the repo is reachable before saving it, so a typo fails once, there and then, rather than in every workspace afterwards. Then **+** next to a project creates a workspace in it: pick a name, and optionally a branch, tag or commit — blank means the project's starting branch. A workspace does not create a branch for you.

Creating one opens a terminal running the bootstrap: update the mirror, clone, check out the branch, run the project's setup command if it has one, then hand the terminal over to Claude Code. You watch all of it happen; if a step fails you're left in a shell in the workspace rather than staring at a tab that vanished.

### Where a workspace starts

A project can name a **default branch** that its workspaces start from; leave it blank and they follow the repo's own default. Either way the branch is checked against the remote when you set it, so a branch that isn't there fails at the dialog rather than in the bootstrap.

Workspaces always start at the *newest* commit on that branch. Two things make that true: the mirror fetches every branch and tag from upstream immediately before the workspace is cloned out of it, and — when the project has no default branch of its own — the server asks the remote which branch its `HEAD` points at *now* rather than trusting the mirror's, so a repo that renames its default is followed instead of silently checked out at the old one.

If the remote can't be reached, the bootstrap says so and falls back to the mirror already on disk. You get a workspace either way; it just may be behind.

Deleting a workspace deletes the directory and kills its terminals, and is not recoverable. Removing a project forgets it and deletes its mirror, but refuses while it still has workspaces — those hold your work.

### The API behind it

```
GET    /api/projects                          projects, each with its workspaces
POST   /api/projects        {repo_url, name?, setup?, default_branch?}
PATCH  /api/projects/{id}   {name?, setup?, default_branch?}
DELETE /api/projects/{id}
POST   /api/workspaces      {project_id, name?, ref?}
DELETE /api/workspaces/{project}/{workspace}
```

## The UI

`GET /` serves a [Solid](https://www.solidjs.com/) app: the project/workspace sidebar on the left, and the workspace you're in filling the rest.

Terminals are laid out with [dockview](https://dockview.dev/) — drag a tab to split the area, drop it back to merge, and the arrangement is remembered per workspace. Each terminal opens its own WebSocket to `/terminal/ws`, which bridges to a PTY running `bash -l` inside the container.

Terminals belong to a workspace. You only see the ones for the workspace you're in; the others keep running in the background, and switching back reattaches to them with their scrollback intact. Opening a workspace with no terminals starts one running Claude Code in it.

Closing a panel kills the terminal behind it. If you close the browser instead (or a restart brings terminals back), **+ terminal** in the top bar lists any that are running without a panel so you can reattach.

**+ editor** opens a VS Code panel for the workspace in the same layout — see [The editor](#the-editor-vs-code-in-a-panel).

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

## The editor (VS Code in a panel)

Every workspace can open a full VS Code beside its terminals — **+ editor** in the top bar, or drag it into a split like any other panel. It's [code-server](https://github.com/coder/code-server), running against that workspace's directory, and it belongs to the workspace: switching workspaces swaps it out, and the arrangement is remembered along with the terminals.

Instances are **per workspace and separate**, so two workspaces of the same project can't disturb each other's editor state. What they *share* is the configuration you'd expect to set once:

```
~/.workbench/vscode/user/settings.json       shared: settings, keybindings, snippets
~/.workbench/vscode/extensions/              shared: installed extensions
~/.workbench/vscode/instances/<digest>/      per workspace: layout, open editors, per-extension state
```

The shared files are symlinked into each instance's user-data-dir. VS Code resolves the link before it writes, so changing a setting *from inside the editor* lands in the shared file and reaches every other workspace — which is the point. The per-workspace directories are separate because sharing one makes concurrent instances collide over `workspaceStorage`.

Extensions come from [Open VSX](https://open-vsx.org/) rather than Microsoft's marketplace, which is a licensing constraint of every non-Microsoft VS Code build, not a choice here. Most things are there (including `Anthropic.claude-code`); Microsoft-licensed ones like Pylance are not.

The colour scheme follows the workbench's own picker, live and with no reload.

### What it costs, and what stops it costing that

The container has 4 GB and one core, and the Claude sessions are the point of the workbench, so the editor is deliberately kept on a short leash:

- **At most two instances run at once.** Opening a third stops the one you used least recently. Its work is on disk in the workspace, and reopening it takes about five seconds.
- **Closing the panel stops the instance**, and an instance nobody is attached to shuts itself down after 30 minutes (`--idle-timeout-seconds`). Without that, VS Code holds its extension host for *three hours* after a disconnect.
- **The bundled Copilot is off** (`chat.disableAIFeatures`). It otherwise spawns a ~200 MB language server per instance, signed out and unasked.

Roughly, measured in this container: ~200 MB idle, 400 MB–1 GB with a browser attached, ~5s to open, and ~2k inotify watches per instance out of a host-wide budget of ~62k that a container can't raise (hence the `files.watcherExclude` defaults). There's no project-wide index to go cold — search is ripgrep on demand — so a brand-new workspace opens as fast as an old one. Language servers are the exception: tsserver and friends rebuild per session regardless, while `rust-analyzer`'s and JDT's caches live in the workspace and are genuinely cold in a fresh clone, exactly as they would be for a fresh `git clone` in a terminal.

### The panel is rendered with dockview's `always` renderer

Not a detail to tidy away: dockview detaches a hidden panel's DOM by default, and
re-attaching an `<iframe>` anywhere else in the document makes the browser reload
it. With the default renderer, every switch between the terminal tab and the
editor tab — and every drag into a split — silently restarted VS Code, losing the
cursor, unsaved buffers and anything running in its terminal. For about five
seconds afterwards hovers and other language features were simply dead, which is
what it looked like from the outside.

`renderer: 'always'` keeps the panel in one stable overlay container instead,
which is what the option is for. Terminals don't need it; xterm re-measures
itself when it comes back.

### How it's served

code-server listens on a unix socket with `--auth none`, and the workbench proxies it at `/vscode/<project>/<workspace>/` on its own origin, so the only way in is through the openhost router's authentication. There is no port to reach it on, and `--disable-proxy` keeps code-server from opening one onto the rest of the container.

```
GET    /api/editor                            what's running, and whether it's installed yet
POST   /api/editor        {workspace_id}      start one (the first call downloads code-server)
DELETE /api/editor/{project}/{workspace}      stop one
```

code-server itself (~740 MB unpacked) is fetched on first use into `~/.workbench/vscode/install/` rather than baked into the image, so it survives rebuilds and costs nothing until someone opens an editor. First ever open takes about 8 seconds including the download.

`code somefile.py` works in the editor's *own* integrated terminal, which knows how to reach the window it's running in. It does not work from the workbench's terminals: that needs a per-window socket the workbench can't know the name of.

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
