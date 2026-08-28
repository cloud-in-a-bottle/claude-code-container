---
name: github-auth
description: Authenticate gh and git inside the workbench using openhost's oauth-v2 app. Use when gh is not logged in, when a git push or clone of a private repo fails with auth errors, when you need to open a PR, or when an oauth token call returns permission_required.
---

# GitHub auth in the workbench

The workbench logs `gh` in by itself at startup and re-mints the token before it expires, so
normally there is nothing to do — check `gh auth status` before assuming otherwise. `gh auth
status` warning that `read:org` is missing is expected and harmless: the grant is `repo`-scoped
and everything except org listings works.

When it really is logged out, the grant is usually missing.

**Read the "GitHub auth" section of `/app/README.md` first** (that's this repo's `README.md`, baked
into the image). It has the exact commands for listing
granted accounts, minting a token, and using it — along with the two failures that block almost
every attempt (`account` being required, and `gh auth login --with-token` rejecting the token for
want of `read:org`). Follow it rather than improvising; don't ask the user for a personal access
token until that flow has actually failed.

What follows is only the judgement that doesn't belong in a reference doc.

## Handling `permission_required`

The response carries a `grant_url` that only the user can approve in a browser (a `401
authorization_required` carries an `authorize_url` and works the same way). Give them the URL,
say plainly that you're blocked until they approve, and stop. Do not poll for it — approval can't
be waited into existence, and a retry loop just burns turns. Retry once they say they've done it.

## Don't leak the token

Never print an access token. Write it to a file or a variable, and pipe any command that might echo
it through `sed -E 's/gh[a-z]_[A-Za-z0-9]+/<REDACTED>/g'`. Prefer embedding it in a one-off remote
URL over configuring stored git credentials.

## Before pushing

Check you actually have write access instead of assuming it:

```bash
gh api repos/<owner>/<repo> -q '{push:.permissions.push, admin:.permissions.admin}'
```

Fork when `push` is `false`. And treat pushing a branch, opening a PR, or commenting as
outward-facing: confirm with the user before the first one unless they've already asked for it.

## If a call that worked starts failing

Tokens are short-lived. A sudden 401 usually means re-mint, not a broken setup — the server's
refresh loop will do it within half an hour, or restart the app to force it.

## Committing

git's `user.name` / `user.email` are seeded from the same GitHub account at startup, so commits
work without setup. If `git commit` still fails with "Author identity unknown", gh was never
logged in — fix that rather than setting an identity by hand.

## Don't clobber a login the user made

`~/.workbench/gh-auth-managed` marks `hosts.yml` as the workbench's own. If it's absent and gh is
logged in, those are the user's credentials — likely broader-scoped and longer-lived than anything
you can mint. Leave them alone.
