---
name: github-auth
description: Authenticate gh and git inside the workbench using openhost's oauth-v2 app. Use when gh is not logged in, when a git push or clone of a private repo fails with auth errors, when you need to open a PR, or when an oauth token call returns permission_required.
---

# GitHub auth in the workbench

The workbench mints short-lived GitHub tokens through openhost's `oauth-v2` app. Do not ask the
user for a personal access token until the flow below has actually failed.

Two things trip up almost every attempt, so start by reading them:

1. **`account` is required when minting.** It defaults to `"default"`, which matches no real
   grant, so omitting it returns `permission_required` even when the user has already approved
   access.
2. **Do not use `gh auth login --with-token`.** The minted token is `repo`-scoped and that command
   demands `read:org`, so it fails with *"missing required scope 'read:org'"*. Use `GH_TOKEN`.

## 1. Find the granted account

```bash
curl -s -X POST "$OPENHOST_ROUTER_URL/api/services/v2/call/oauth/accounts" \
  -H "Authorization: Bearer $OPENHOST_APP_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"provider":"github","scopes":["repo"]}'
```

Returns `{"accounts":["<login>", ...]}`, often repeating the same login — dedupe it. An empty list
means nothing is granted yet; go to step 3.

## 2. Mint the token

```bash
curl -s -X POST "$OPENHOST_ROUTER_URL/api/services/v2/call/oauth/token" \
  -H "Authorization: Bearer $OPENHOST_APP_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"provider":"github","scopes":["repo"],"account":"<login>"}'
```

On success the response has `access_token`. Never print it — write it to a file or a variable, and
redact it from any command output you show the user.

## 3. If you get `permission_required`

The response body contains a `grant_url`. Give the user that URL and ask them to approve it in a
browser; only they can. Append `return_to=/` if it is empty — the provider ignores any `return_to`
that does not start with `/`.

Approval is not something you can poll into existence, and repeatedly retrying is just noise. Ask,
wait for the user to say they have done it, then retry step 1. Confirm what landed with:

```bash
oh curl -- -s "https://$OPENHOST_ZONE_DOMAIN/api/permissions/v2?app_id=$OPENHOST_APP_ID"
```

Grants show up there with their `scope` (`app` or `global`) and the `account` they are tied to.

## 4. Use the token

```bash
export GH_TOKEN=$(cat /path/to/token)
gh api user -q .login
gh api repos/<owner>/<repo> -q '{push:.permissions.push, admin:.permissions.admin}'
```

Check `push` before assuming you can push a branch; fork instead when it is `false`.

For git itself, embed the token in the remote URL for a single command rather than storing
credentials:

```bash
git push "https://x-access-token:$GH_TOKEN@github.com/<owner>/<repo>.git" <branch>
```

Pipe output through `sed -E 's/gh[a-z]_[A-Za-z0-9]+/<REDACTED>/g'` so a token cannot leak into the
transcript.

## Notes

- Tokens expire. If a call that worked earlier starts returning 401, re-mint from step 2.
- `seed_gh_auth()` in `remote_services.py` is meant to do this at startup but currently requests a
  token without an `account`, so it silently no-ops. That is why `gh` is usually logged out.
- Opening a PR, pushing, or anything else that leaves the machine is the user's call. Confirm
  before the first such action unless they have already asked for it.
