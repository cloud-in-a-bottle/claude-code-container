---
name: attach-repo
description: Turn a workbench folder project (one with no repo, whose workspaces started as empty directories) into an ordinary repo project — git init the workspace, create or connect its GitHub repo, push, and point the project at it so new workspaces are clones. Use when the user wants to put their work in a repo, "make this a real project", give the project a repo, create a GitHub repo for what they've built, or says the repo for this folder project now exists.
---

# Attach a repo to a folder project

A **folder project** in the workbench has no `repo_url`; its workspaces live at `~/workspaces/<project>/<workspace>` and started empty. Once the work there deserves a repo, the project can be given one, once: from then on it is an ordinary repo project, and new workspaces are clones of that repo. The "Folder projects" section of `/app/README.md` has the background.

The workbench API is at `http://127.0.0.1:${PORT:-5000}`, reachable from any terminal here without auth.

## 1. Work out where you are, and check it's a folder project

The project and workspace are the two path segments after `~/workspaces/`. Work from the workspace root, not a subdirectory.

```bash
curl -sS "http://127.0.0.1:${PORT:-5000}/api/projects" | jq '.[] | select(.id == "<project>")'
```

If `repo_url` is already non-empty, this is a repo project and there is nothing to attach — say so and stop.

If the workspace root holds several repos side by side (subdirectories with their own `.git`) rather than one body of work, this project is a multi-repo workspace and isn't meant to become a single repo. Ask the user before going further; don't fold clones of other repos into a new one.

## 2. Ask the user what the repo should be

Creating a repo and pushing to it is outward-facing, so confirm before doing it:

- **A repo that already exists?** Then get its URL and skip the creation in step 4.
- **Otherwise:** which owner (their user or an org — `gh api user -q .login` and `gh org list` show the options), what name (suggest the project id), and **private or public** (suggest private; never make one public without being told to).

## 3. Make the workspace a repo with a commit

```bash
git init -b main          # only if there's no .git yet
git status --short
```

Before the first commit, make sure a `.gitignore` keeps out what shouldn't be published: dependency and build dirs (`node_modules/`, `.venv/`, `dist/`…), and anything holding secrets (`.env`, keys, tokens). Show the user what will be committed if any of it looks doubtful. Then commit.

## 4. Create or connect the remote, and push

`gh` is normally logged in already; if it isn't, or a push fails on auth, use the `github-auth` skill rather than asking for a token. Use an **https** URL throughout — it is what the workbench authenticates with.

```bash
# a new repo
gh repo create <owner>/<name> --private --source . --remote origin --push
# or an existing one (it must be empty, or you're joining its history -- check with the user)
git remote add origin https://github.com/<owner>/<name>.git && git push -u origin HEAD
```

Then let git know the remote's default branch. The sidebar measures a workspace's own commits against `origin/HEAD`, which `git init` + `push` doesn't set:

```bash
git remote set-head origin --auto
```

## 5. Every other workspace in the project must be a repo too

Once the project has a repo, a workspace directory that isn't a repo with at least one commit reads as "still being cloned" forever, so the workbench refuses the attach while any exist. List the project's workspaces (step 1's output, `.workspaces[].path`) and check each with `git -C <path> rev-parse --verify HEAD`. For any that fail, ask the user: commit there, fold its contents into this one, or delete it (`DELETE /api/workspaces/<project>/<workspace>` — not recoverable, so only on their say-so).

## 6. Attach it

Use exactly the URL `git remote get-url origin` reports, with `.git` on the end for https:

```bash
curl -sS -X PATCH "http://127.0.0.1:${PORT:-5000}/api/projects/<project>" \
  -H 'Content-Type: application/json' \
  -d '{"repo_url": "https://github.com/<owner>/<name>.git"}'
```

Add `"default_branch": "<branch>"` only if new workspaces should start somewhere other than the repo's default. Errors say what's wrong:

- `404` "nothing has been pushed to it yet" — the push in step 4 didn't land, or the URL is off.
- `409 not_a_repo` — step 5 isn't done; the message names the workspaces.
- `409 already_a_project` — another project already has this repo. Tell the user; they may want to delete one of the two.

This can't be undone from the UI, which is why it comes last.

## 7. Tell the user what changed

The project is now a repo project: the sidebar's ⚙ shows its Git URL, **+** makes workspaces that are fresh clones of the repo (the first one also creates the project's local mirror), and the workspace they're in carries on as it was, now with normal git status in the sidebar.
