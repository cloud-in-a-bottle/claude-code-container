---
name: side-by-side
description: Turn the workbench's side-by-side panel on or off — a resizable pane next to the terminal that loads any URL in an iframe, useful for watching a dev server or another openhost app while you work. Use when the user asks to enable/disable/toggle the side panel, split view, side-by-side view, preview pane, or asks to see a web page next to the terminal.
---

# Side-by-side panel

A resizable pane beside the terminal that loads any URL. Off by default; the setting is stored in
`$HOME/.workbench/ui.json`, which lives on the app's persistent data volume, so it survives
container rebuilds.

## Turning it on

```bash
curl -sS -X POST "http://127.0.0.1:${PORT:-5000}/api/ui/settings" \
  -H 'Content-Type: application/json' -d '{"side_panel": true}'
```

## Turning it off

```bash
curl -sS -X POST "http://127.0.0.1:${PORT:-5000}/api/ui/settings" \
  -H 'Content-Type: application/json' -d '{"side_panel": false}'
```

## Checking the current state

```bash
curl -sS "http://127.0.0.1:${PORT:-5000}/api/ui/settings"
```

Each call returns the settings as they were saved, e.g. `{"side_panel": true}`.

## After running it

**Tell the user to reload the workbench page.** The panel is wired into `index.html` when that page
is served, so the change lands on the next load, not the current one. Reloading is safe — terminals
live server-side, so the page re-attaches to the running session and replays its scrollback.

## Using the panel

- The URL bar at the top of the pane loads any address; press Enter.
- Drag the divider to resize, or double-click it to reset. It's also focusable, with arrow keys
  (Shift for larger steps).
- `↗` opens the current URL in a real browser tab, `×` hides the pane, and **◻ panel** in the tab
  bar brings it back.
- Width, visibility and last URL are remembered per browser in `localStorage`; the on/off setting
  above is server-side and shared across browsers.

## Notes

- Hiding the pane with `×` is not the same as turning the feature off — hiding is per-browser,
  while this skill controls whether the panel is loaded at all.
- Sites that send `X-Frame-Options: DENY` or a restrictive `frame-ancestors` will refuse to render
  in the iframe. That's the site's choice and can't be worked around from here; use `↗` to open it
  in a real tab instead.
