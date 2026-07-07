# Handoff: SSH-over-chisel into the claude-workbench

_Written 2026-07-07. Paused before deploying to the live instance._

## 1. Your explicit instructions

Given verbatim across the session:

1. "load the openhost skill."
2. "fork `https://github.com/imbue-openhost/claude-code-container` to a new repo in order to
   add the feature: chisel proxy into sshd running inside the workbench? so we can connect from
   external?"
3. "check github now, it should work. you should not need ports, you can use chisel (search it)
   to tunnel the ssh port" — i.e. **no extra host ports**; tunnel SSH over the existing HTTP(S)
   subdomain using chisel.
4. "update me — what's up next? did we already deploy the changes to
   https://claude-workbench2.oh.bowei.in/ (the sandbox)?"
5. "pause here — write a doc containing all my explicit instructions, what you did and figured
   out, any hiccups along the way, and what's up next" (this document).

### Two clarifying questions I asked that you did NOT answer (I proceeded on defaults)

- **Repo strategy** — I recommended a standalone repo; reality turned out different (see §3). Net
  effect: work lives on branch `chisel-ssh` of your existing fork `boweiliu/claude-code-container`.
- **SSH key provisioning** — I defaulted to **both** methods: seed from an optional
  `SSH_AUTHORIZED_KEYS` secret AND let you paste a key into `~/.ssh/authorized_keys` in a terminal.

If either default is wrong, say so and I'll adjust.

## 2. What was built (the feature)

**Design: chisel as the container front-door.** openhost only routes HTTP/WebSocket to one
container port and opens no extra ports — so chisel binds that port and:
- reverse-proxies all normal traffic to the Quart terminal UI (moved to a loopback backend port),
  so the browser experience is unchanged;
- accepts chisel tunnel connections on the public `/_chisel` path;
- forwards tunneled traffic to an in-container `sshd` listening on `127.0.0.1:22` only (never bound
  to a host port — the tunnel is the only way in).

```
your machine                      openhost (HTTPS)            claude-workbench container
┌────────────┐   chisel/wss      ┌──────────────┐  proxy     ┌───────────────────────┐
│ ssh client │──── /_chisel ────▶│ router :443   │───────────▶│ chisel :PORT           │
│ chisel cli │                   └──────────────┘            │   ├─ /_chisel → tunnel │
└────────────┘                                               │   └─ else → quart :5001│
      └── localhost:2222 ───────────(tunnel)─────────────────▶│ sshd 127.0.0.1:22      │
                                                              └───────────────────────┘
```

**Auth = two independent layers** (the openhost session cookie deliberately does NOT gate this —
the external client has no browser login, which is exactly why `/_chisel` is a `public_path`):
1. chisel `--auth user:pass` — auto-generated + persisted on first boot, or pin via `CHISEL_AUTH` secret.
2. SSH key auth — `sshd` is key-only (`PermitRootLogin prohibit-password`, `PasswordAuthentication no`).

### Files (commit `1ca2def` on branch `chisel-ssh`)

| File | Change |
|------|--------|
| `tunnel.sh` (new) | Supervisor. Provisions persistent sshd host key, `authorized_keys` (user + secret), and a persisted chisel credential; starts sshd + Quart backend + chisel front-door; exits non-zero if any dies so openhost restarts clean. |
| `sshd_config` (new) | Key-only, loopback-only, `UsePAM no`. Runtime paths passed as `-o`/`-h` overrides. |
| `scripts/ssh-connect.sh` (new) | Client-side helper you run on your own machine: opens the tunnel then `ssh`. |
| `Dockerfile` | Install `openssh-server` + chisel v1.11.7; copy new files; `mkdir /run/sshd`. |
| `openhost.toml` | `public_paths=["/_chisel"]`; secret grants `SSH_AUTHORIZED_KEYS`, `CHISEL_AUTH`; version → `0.2.0`. |
| `entrypoint.sh` | Final `exec` now hands off to `tunnel.sh` instead of `server.py`. |
| `config.py` / `server.py` | New `BIND_HOST` env so the backend binds `127.0.0.1` behind chisel (default `0.0.0.0` for standalone `python server.py`). |
| `workbench.sh` | Terminal login banner: shows the chisel credential + a ready-to-paste connect command, warns if no SSH key is installed. |
| `test_ssh_tunnel.py` (new) | Regression tests: manifest wiring, `BIND_HOST`, shell syntax, sshd policy. |
| `pyproject.toml` | mypy override for the new test module. |

### How to connect once deployed

```bash
# Get CHISEL_AUTH from the workbench terminal login banner; add your pubkey first
# (~/.ssh/authorized_keys in a terminal, or the SSH_AUTHORIZED_KEYS secret).
CHISEL_AUTH='workbench:xxxx' ./scripts/ssh-connect.sh https://claude-workbench2.oh.bowei.in
# or by hand:
chisel client --auth 'workbench:xxxx' https://claude-workbench2.oh.bowei.in/_chisel 2222:localhost:22 &
ssh -p 2222 root@localhost
```

## 3. What I figured out along the way

- **openhost routing model** (from the local openhost clone + router source): subdomain → one
  container port, HTTP + WebSocket proxied, no extra ports needed. `public_paths` bypasses auth for
  **both** HTTP and WS handshakes (verified in
  `compute_space/.../web/middleware/subdomain_proxy.py` and `core/apps.py:is_public_path`, which is
  a prefix match). This is what makes the `/_chisel` public-path approach valid.
- **chisel internals** (verified by reading `server/server_handler.go` + `server/server.go`, not
  just docs): with `--backend` set, chisel proxies **everything** non-chisel — including `/health`
  and non-chisel WebSockets — to the backend via Go's `httputil.NewSingleHostReverseProxy` (which
  handles WS upgrades since Go 1.12). Its built-in `/health` only fires when NO `--backend` is set,
  so openhost's health check correctly reaches the real Quart `/health`. The chisel handshake is
  detected by the `Sec-WebSocket-Protocol` header on **any** URL path, so serving it under
  `/_chisel` behind the router works.
- **Deploy mechanics** (from `compute_space_cli` + `add_app.html`): `oh app deploy REPO_URL`
  supports a **branch/ref via `@ref` suffix** — e.g. `.../claude-code-container@chisel-ssh`. So the
  feature can be deployed straight from the branch; no merge to `main` required first. New secret
  grants require `--grant-permissions-v2` on deploy.
- **This environment IS the sandbox.** We're running inside `claude-workbench2` at `oh.bowei.in`
  (`OPENHOST_APP_NAME=claude-workbench2`). The `oh` CLI here is configured and logged in. The app
  currently runs `main @ 31aa4db` — upstream imbue-openhost, the exact commit I branched from.

## 4. Hiccups

- **GitHub auth** wasn't present at first (`gh` not logged in, no token) — blocked repo writes.
  Resolved once you enabled it (now logged in as `boweiliu`, `repo` scope).
- **A fork already existed.** `boweiliu/claude-code-container` was already a GitHub fork of the
  upstream, but its `main` was **stale** (`dc77820`, older than upstream `31aa4db`). So instead of
  creating a duplicate repo or clobbering that `main`, I based the feature on current upstream HEAD
  and pushed it as branch `chisel-ssh`. **The fork's `main` is still stale and does NOT contain the
  feature.**
- **`git push` failed** initially — `git` had no credential helper even though `gh` was logged in.
  Fixed with `gh auth setup-git`.
- **mypy strictness** — `disallow_any_generics` rejected a bare `dict` in the new test; fixed with
  an explicit type + a per-module mypy override (mirroring the existing `test_open_workspace`
  override).
- **Couldn't build/run the real container here** (no rootless podman + openhost locally), so I
  verified the *mechanism* end-to-end with the real chisel binary against the real Quart app
  instead (see §5).

## 5. Verification done

- `ruff check`, `ruff format --check`, `mypy` — all clean. `pytest` — **64 passed** (59 original +
  5 new).
- **End-to-end with the real chisel binary + real Quart app** (locally, on loopback):
  1. `/health` proxies through `chisel --backend` → `{"status":"ok"}`.
  2. The terminal WebSocket (`/terminal/ws`) upgrades through `--backend` → `HTTP/1.1 101` (browser
     UI unaffected).
  3. A chisel client, authenticating with `--auth`, tunnels over the `/_chisel` path to a stand-in
     sshd (TCP echo) and gets a round-trip.

### NOT yet verified (the one real runtime risk)

`sshd` privilege separation under **rootless podman + `--cap-drop=ALL` + `--security-opt=no-new-privileges`**.
The default cap baseline includes `SETUID`/`SETGID`, so it *should* work, but this can only be
confirmed on a real openhost deploy. `sshd -e` logs to stderr → visible in `oh app logs`.

## 6. What's up next (open decisions for you)

**Nothing is deployed.** Branch is pushed; live `claude-workbench2` is untouched.

Deciding how to deploy — options, safest first:

1. **Deploy as a SEPARATE app from the branch (recommended).** Leaves this workbench alive as a
   fallback so a broken image can't lock us out, and lets us test SSH on the new app before
   promoting:
   ```bash
   oh app deploy https://github.com/boweiliu/claude-code-container@chisel-ssh \
     --name claude-workbench-ssh --grant-permissions-v2 --wait
   ```
2. **Replace/reload `claude-workbench2` in place** — ⚠️ this rebuilds and **restarts the container
   this session runs in**. If the new image fails health checks (e.g. sshd/chisel supervisor
   issue), the workbench may not come back and we lose the browser UI. Only do this after option 1
   proves the image is healthy.
3. **Promote to the fork's `main`** — fast-forward/merge `chisel-ssh` into `boweiliu` `main` (also
   un-stales it), then deploy from `main`. Tidy but not required to ship.

Also open (from §1): confirm the repo strategy and SSH-key-provisioning defaults if you want them
changed. Neither a PR nor any change to the fork's `main` has been made — I stopped before taking
any of the above deploy/promote actions, pending your go-ahead.

## Key references

- Branch: `chisel-ssh` @ `1ca2def` → https://github.com/boweiliu/claude-code-container/tree/chisel-ssh
- New-PR link: https://github.com/boweiliu/claude-code-container/pull/new/chisel-ssh
- Local checkout: `~/my_project/claude-code-container`
- Live app: `claude-workbench2` @ `oh.bowei.in`, currently `main @ 31aa4db` (upstream)
