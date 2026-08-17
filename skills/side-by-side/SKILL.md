---
name: side-by-side
description: Turn the workbench's side-by-side panel on or off — a resizable pane next to the terminal that loads any URL in an iframe, useful for watching a dev server or another openhost app while you work. Use when the user asks to enable/disable/toggle the side panel, split view, side-by-side view, preview pane, or asks to see a web page next to the terminal.
---

# Side-by-side panel

Off by default. See the "Side-by-side panel" section of `/app/README.md` (this repo's `README.md`,
baked into the image) for what it is, the controls, and where the setting is stored.

## Toggling it

```bash
# on  (use false to turn it off)
curl -sS -X POST "http://127.0.0.1:${PORT:-5000}/api/ui/settings" \
  -H 'Content-Type: application/json' -d '{"side_panel": true}'

# current state
curl -sS "http://127.0.0.1:${PORT:-5000}/api/ui/settings"
```

Each call returns the settings as saved, e.g. `{"side_panel": true}`.

## Then tell the user to reload the page

The panel is wired in when `/` is served, so the change lands on the next load, not the current
one. Say so explicitly — otherwise it looks like nothing happened. Reloading is safe: terminals
live server-side, so the page re-attaches to the running session and replays its scrollback.

## Worth mentioning if it comes up

- Hiding the pane with `×` is per-browser and is not the same as turning the feature off here.
- Sites sending `X-Frame-Options: DENY` or a restrictive `frame-ancestors` won't render in the
  iframe. That's their choice and can't be worked around; the `↗` button opens the URL in a real
  tab instead.
