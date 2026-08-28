# claude-workbench

You are running inside the claude-workbench container: an openhost app that provides in-browser terminals and several independent copies of a repo. These notes apply everywhere in this container; per-repo `claude.md` files still apply on top of them, and win where they disagree.

- work happens in workspaces at `~/workspaces/<project>/<workspace>`. each workspace is a full copy of the project's repo — there is no canonical checkout — so paths from one workspace never point into another. stay in the workspace you were opened in unless asked otherwise.
- `$HOME` is the openhost app-data dir, which is the only thing that survives a redeploy. `/app` is the running image and is replaced on every app update: never write anything there that needs to last.
- `~/claude-code-container` is a checkout of the workbench's own source, kept for convenience. it tracks the default branch, so it can be *ahead of* the image actually running, and it is reset with `git reset --hard` on the next app update. don't do real work there — open a workspace instead.
- `~/openhost` is a clone of the openhost platform itself, and `~/app-template` the starter app. both are reference material: read them, don't edit them.
- the `oh` cli talks to openhost instances. `oh instance list` shows them. the user will say which instance to use; leave the others alone. these instances face the public internet, so treat anything that widens public access (eg `public_paths` in `openhost.toml`) as needing explicit confirmation.
- the container is root under rootless podman with `cap-drop=ALL` and `no-new-privileges`, so `apt-get install` works directly and `sudo` does not. the sandbox is why claude runs with `--dangerously-skip-permissions` here; that is not a licence to be careless with the user's repos or credentials.
- if `gh` or `git` hits an auth error, use the `github-auth` skill rather than asking the user for a token. the `openhost` skill has the curated platform docs; `side-by-side` opens a preview pane next to the terminal.

Personal additions belong in `~/.claude/CLAUDE.local.md`, which is imported below and is yours to edit. Don't edit `~/.claude/CLAUDE.md` itself — it is a symlink to `/app/claude-home/CLAUDE.md` inside the image, so the next app update replaces it.

@~/.claude/CLAUDE.local.md
