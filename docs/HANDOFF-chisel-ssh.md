# Handoff: SSH-over-chisel into the claude-workbench

_Written 2026-07-07. Paused before deploying to the live instance._

This doc is deliberately split into three parts so the boundaries are unambiguous:

- **Part A — What you asked** — your explicit instructions, verbatim. Nothing inferred.
- **Part B — What I discovered** — objective facts I learned by reading code, running commands, and
  testing. True regardless of who's driving.
- **Part C — What I decided** — choices I made on my own judgement (including where I departed from
  what I'd earlier recommended, and defaults I picked because a question went unanswered).

A short **Reference** section at the end describes the resulting feature and how to use it.

---

## Part A — What you asked (explicit instructions, verbatim)

1. "load the openhost skill."
2. "fork `https://github.com/imbue-openhost/claude-code-container` to a new repo in order to add
   the feature: chisel proxy into sshd running inside the workbench? so we can connect from
   external?"
3. "check github now, it should work. you should not need ports, you can use chisel (search it) to
   tunnel the ssh port"
4. "update me — what's up next? did we already deploy the changes to
   https://claude-workbench2.oh.bowei.in/ (the sandbox)?"
5. "pause here — write a doc containing all my explicit instructions, what you did and figured out,
   any hiccups along the way, and what's up next."
6. "commit the doc, and then afterwards, edit the doc to make sure we have a clear boundary between
   what was asked of you, what you discovered, and what decisions you made based on your judgement."
   … "and push".

**Constraints you stated explicitly** (extracted from the above, not added by me):
- New repo — a fork of the named upstream.
- Feature — chisel tunnel/proxy into an `sshd` running inside the workbench, reachable externally.
- **No extra host ports** — tunnel SSH over the existing HTTP(S) subdomain using chisel.

**Questions I put to you that you did NOT answer** (so I chose defaults — see Part C):
- Repo strategy (standalone new repo vs true GitHub fork vs renamed).
- How your SSH public key should get into the workbench (secret vs manual vs both).

---

## Part B — What I discovered (facts)

### About this environment
- **This environment IS the sandbox.** We are running inside the `claude-workbench2` app at
  `oh.bowei.in` (`OPENHOST_APP_NAME=claude-workbench2`, `OPENHOST_ZONE_DOMAIN=oh.bowei.in`).
- The `oh` CLI here is configured and logged into `oh.bowei.in`.
- `claude-workbench2` currently runs **`main @ 31aa4db`** — i.e. upstream imbue-openhost at the
  exact commit the feature branch was based on. **It is unchanged; nothing was deployed.**

### About GitHub / the repo (some of these are also hiccups)
- GitHub auth was **absent at first** (`gh` not logged in, no `GH_TOKEN`), which blocked all repo
  writes. After you enabled it, `gh` is logged in as **`boweiliu`** with `repo` scope.
- **A fork already existed**: `boweiliu/claude-code-container` is already a GitHub fork of the
  upstream, but its `main` is **stale** (`dc77820`, older than upstream `31aa4db`).
- `git push` initially failed — `git` had no credential helper even though `gh` was authenticated.
  `gh auth setup-git` fixed it.

### About openhost (from the local clone + router source)
- Subdomain routes to a **single** container port; both HTTP and WebSocket are proxied; no extra
  ports are needed for the design.
- `public_paths` **bypasses auth for both HTTP and WebSocket handshakes** — verified in
  `web/middleware/subdomain_proxy.py` and `core/apps.py:is_public_path` (a prefix match). This is
  what makes serving the tunnel on a public `/_chisel` path viable.
- `oh app deploy REPO_URL` accepts a **branch/ref via an `@ref` suffix** (per the CLI and
  `add_app.html`), e.g. `…/claude-code-container@chisel-ssh` — so the feature can be deployed
  straight from the branch without merging to `main`. New secret grants require
  `--grant-permissions-v2` on deploy.

### About chisel (from reading `server/server_handler.go` + `server/server.go`, not just docs)
- With `--backend` set, chisel proxies **everything** non-chisel — including `/health` and
  non-chisel WebSockets — to the backend via Go's `httputil.NewSingleHostReverseProxy` (which
  handles WS upgrades since Go 1.12).
- chisel's built-in `/health` endpoint only fires when NO `--backend` is set, so openhost's health
  check correctly reaches the real Quart `/health`.
- The chisel handshake is detected by the `Sec-WebSocket-Protocol` header on **any** URL path, so
  serving it under `/_chisel` behind the router works.
- Latest release is **v1.11.7**; assets are gzipped single binaries named
  `chisel_1.11.7_linux_<arch>.gz` (amd64/arm64 available).

### Verification results
- `ruff check`, `ruff format --check`, `mypy` — all clean. `pytest` — **64 passed** (59 original +
  5 new).
- **End-to-end with the real chisel binary + real Quart app** (locally, on loopback):
  1. `/health` proxies through `chisel --backend` → `{"status":"ok"}`.
  2. The terminal WebSocket (`/terminal/ws`) upgrades through `--backend` → `HTTP/1.1 101`.
  3. A chisel client, authenticating with `--auth`, tunnels over `/_chisel` to a stand-in sshd and
     gets a round-trip.
- **NOT yet verified:** `sshd` privilege separation under rootless podman +
  `--cap-drop=ALL` + `--security-opt=no-new-privileges`. The default cap baseline includes
  `SETUID`/`SETGID` so it *should* work, but only a real deploy confirms it. (`sshd -e` logs to
  stderr → visible in `oh app logs`.) This is the one real runtime risk.

---

## Part C — What I decided (my judgement)

Each item is a choice I made, not something you asked for. Rationale included so you can overrule.

1. **Architecture: chisel as the container front-door** (chisel binds the app port, `--backend`
   reverse-proxies normal traffic to the Quart UI on a loopback port; tunnel served on `/_chisel`;
   `sshd` on `127.0.0.1:22`). _Why:_ it needs no new Python dependency and no fragile WebSocket-
   proxy code — chisel's `--backend` is purpose-built for this, and I verified in source that it
   passes the app's own WebSocket + `/health` through untouched. The alternative (keep Quart in
   front and proxy `/_chisel` to a local chisel via a Python WS client) was rejected as more code
   and more failure surface.
2. **Used the existing fork + a feature branch, based on current upstream HEAD.** Because a fork
   already existed with a stale `main`, I created branch **`chisel-ssh`** off upstream `31aa4db`
   and pushed it there — rather than creating a duplicate repo or force-updating that `main`.
   **This departs from the "standalone new repo" option I'd earlier recommended**, because reality
   (a pre-existing fork) made the branch approach the least destructive. The fork's `main` is
   **still stale and does not contain the feature.**
3. **SSH key provisioning = "both".** You didn't answer my question, so I implemented both: seed
   `~/.ssh/authorized_keys` from an optional `SSH_AUTHORIZED_KEYS` secret AND let you paste a key in
   a terminal. Both persist across redeploys (HOME is on the data dir).
4. **Security boundary = `public_paths=["/_chisel"]` + chisel `--auth` + key-only sshd.** The
   openhost session cookie can't gate the tunnel (the external client has no browser login), so I
   made only `/_chisel` public and layered chisel's own credential and SSH key auth as the two
   real gates. `sshd` is loopback-only, key-only, `UsePAM no`.
5. **Persistence + login ergonomics.** I persist the sshd host key, `authorized_keys`, and an
   auto-generated chisel credential under `$HOME`; `usermod` points root's home at the data dir so
   an SSH session lands in the same place as the browser terminals; and I added a terminal login
   banner that prints the connect command + credential.
6. **Fail-loud supervisor.** `tunnel.sh` exits non-zero if sshd, the backend, or chisel dies, so
   openhost restarts the container rather than leaving a half-up workbench. (Matches the repo's
   stated "fail loudly" style.)
7. **Pinned chisel to v1.11.7** and added regression tests (`test_ssh_tunnel.py`) + a mypy override
   for that test module.
8. **Did NOT deploy, and recommend deploying as a *separate* app first.** Because deploying/
   reloading `claude-workbench2` restarts the very container this session runs in — with a lockout
   risk if the new image is unhealthy — I stopped and recommend option 1 below.
9. **Doc placement.** I first wrote this handoff *outside* the repo (it's status, not feature), then
   moved it to `docs/` and committed it per your later instruction.

---

## What's up next (open decisions for you)

**Nothing is deployed.** Branch is pushed; live `claude-workbench2` is untouched. Deploy options,
safest first:

1. **Deploy as a SEPARATE app from the branch (recommended).** Keeps this workbench alive as a
   fallback and lets us test SSH before promoting:
   ```bash
   oh app deploy https://github.com/boweiliu/claude-code-container@chisel-ssh \
     --name claude-workbench-ssh --grant-permissions-v2 --wait
   ```
2. **Replace/reload `claude-workbench2` in place** — ⚠️ rebuilds and **restarts the container this
   session runs in**; if it fails health checks the browser UI may not come back. Only after
   option 1 proves the image healthy.
3. **Promote to the fork's `main`** — fast-forward/merge `chisel-ssh` into `boweiliu` `main` (also
   un-stales it), then deploy from `main`.

Also open: confirm the Part C #2 (repo) and #3 (SSH-key) defaults if you'd like them changed.

---

## Reference — the feature

**How it works.** chisel binds the app port; reverse-proxies normal traffic to the Quart terminal
UI (moved to a loopback backend port), so the browser experience is unchanged; accepts chisel
tunnels on the public `/_chisel` path; forwards them to an in-container `sshd` on `127.0.0.1:22`.

```
your machine                      openhost (HTTPS)            claude-workbench container
┌────────────┐   chisel/wss      ┌──────────────┐  proxy     ┌───────────────────────┐
│ ssh client │──── /_chisel ────▶│ router :443   │───────────▶│ chisel :PORT           │
│ chisel cli │                   └──────────────┘            │   ├─ /_chisel → tunnel │
└────────────┘                                               │   └─ else → quart :5001│
      └── localhost:2222 ───────────(tunnel)─────────────────▶│ sshd 127.0.0.1:22      │
                                                              └───────────────────────┘
```

**Connect (once deployed).** Get `CHISEL_AUTH` from the workbench terminal login banner; add your
pubkey first (`~/.ssh/authorized_keys` in a terminal, or the `SSH_AUTHORIZED_KEYS` secret):
```bash
CHISEL_AUTH='workbench:xxxx' ./scripts/ssh-connect.sh https://claude-workbench2.oh.bowei.in
# or by hand:
chisel client --auth 'workbench:xxxx' https://claude-workbench2.oh.bowei.in/_chisel 2222:localhost:22 &
ssh -p 2222 root@localhost
```

**Files (commit `1ca2def` on branch `chisel-ssh`).**

| File | Change |
|------|--------|
| `tunnel.sh` (new) | Supervisor: provisions persistent host key + authorized_keys + chisel credential; starts sshd + Quart backend + chisel front-door; exits if any dies. |
| `sshd_config` (new) | Key-only, loopback-only, `UsePAM no`; runtime paths via `-o`/`-h`. |
| `scripts/ssh-connect.sh` (new) | Client-side helper: opens the tunnel then `ssh`. |
| `Dockerfile` | Install `openssh-server` + chisel v1.11.7; copy new files; `mkdir /run/sshd`. |
| `openhost.toml` | `public_paths=["/_chisel"]`; grants `SSH_AUTHORIZED_KEYS`, `CHISEL_AUTH`; v0.2.0. |
| `entrypoint.sh` | Final `exec` hands off to `tunnel.sh`. |
| `config.py` / `server.py` | New `BIND_HOST` so the backend binds loopback behind chisel. |
| `workbench.sh` | Login banner with the chisel credential + ready-to-paste connect command. |
| `test_ssh_tunnel.py` (new) | Regression tests: manifest wiring, `BIND_HOST`, shell syntax, sshd policy. |
| `pyproject.toml` | mypy override for the new test module. |

**Key references.**
- Branch: `chisel-ssh` @ `1ca2def` → https://github.com/boweiliu/claude-code-container/tree/chisel-ssh
- New-PR link: https://github.com/boweiliu/claude-code-container/pull/new/chisel-ssh
- Local checkout: `~/my_project/claude-code-container`
- Live app: `claude-workbench2` @ `oh.bowei.in`, currently `main @ 31aa4db` (upstream)
