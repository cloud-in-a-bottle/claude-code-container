import os
import urllib.parse

# The GitHub org every triggered issue is expected to belong to. Issues in the Linear project all
# map to a repo here, so this is the whole candidate set the resolver ever chooses from.
GITHUB_ORG = os.environ.get("LINEAR_GITHUB_ORG", "cloud-in-a-bottle")

# What has to appear in a comment for it to start a run. Matched case-insensitively, and against
# Linear's `@[name](id)` mention markup as well as plain text, so it works whether or not a Linear
# user by this name exists to be mentioned for real.
TRIGGER_PHRASE = os.environ.get("LINEAR_TRIGGER_PHRASE", "@claude")

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
WEBHOOK_PATH = "/api/linear/webhook"

# Deliveries older than this are refused. Linear signs the body but a signature stays valid
# forever, so without a freshness check a captured delivery could be replayed indefinitely.
MAX_DELIVERY_AGE_SECONDS = 300

# Below this, the resolver says so in Linear instead of starting work in a repo it guessed at.
MIN_REPO_CONFIDENCE = 0.6

# How often to check whether a run's PR has merged, so its workspace can be deleted.
REAP_INTERVAL_SECONDS = 300


def app_base_url() -> str:
    """This workbench's own URL, for the workspace deep link a run puts in its PR.

    Built from the environment openhost gives every app rather than configured, so it stays correct
    if the app is renamed or the zone moves. Raises when either piece is missing: a run that
    silently linked to `https://None/` would look fine right up until someone clicked it.
    """
    app = os.environ.get("OPENHOST_APP_NAME", "")
    zone = os.environ.get("OPENHOST_ZONE_DOMAIN", "")
    if not app or not zone:
        raise RuntimeError("OPENHOST_APP_NAME and OPENHOST_ZONE_DOMAIN must be set to build the workspace URL")
    return f"https://{app}.{zone}"


def workspace_url(workspace_id: str) -> str:
    """A deep link into the workspace's tab. Owner-gated by the router, which is what we want —
    this goes in a public PR body, and only the owner should be able to follow it."""
    return f"{app_base_url()}/?workspace={urllib.parse.quote(workspace_id)}"
