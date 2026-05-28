# claude-workbench

An openhost app that gives you tabbed in-browser terminals, preinstalled
Claude Code, and a cloned copy of the openhost repo. Meant as a starting
point for building or debugging openhost apps.

## What's inside the container

- `@anthropic-ai/claude-code` (npm, installed at image build time).
- Python 3 + git + the usual tools.
- A clone of `https://github.com/imbue-openhost/openhost` placed at
  `~/openhost` on first container start (override with `OPENHOST_REPO_URL`
  or `OPENHOST_DIR` env vars).
- A Claude Code skill at `~/.claude/skills/openhost/` that points Claude
  at the curated docs in the local openhost clone.

Authentication for `claude` is whatever the user sets up inside the
terminal — either `ANTHROPIC_API_KEY` in the environment or an interactive
`claude login`. The workbench doesn't manage that.

As a convenience, if the `secrets-v2` app is installed and `ANTHROPIC_API_KEY`
is set there, the workbench fetches it on first PTY launch and exports it
into every new terminal's environment. This is best-effort — if the secrets
app isn't around the terminal still works, you just have to set the key
yourself.

## The UI

`GET /` serves a tabbed xterm.js page. Each tab opens its own WebSocket
to `/terminal/ws`, which bridges to a PTY running `bash -l` inside the
container.

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
  repo=<clone-url>&ref=<commit|tag|branch>
  -> 303 redirect to /?session=<token>
```

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

Then open http://localhost:5000.
