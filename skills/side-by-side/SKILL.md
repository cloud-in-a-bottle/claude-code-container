---
name: side-by-side
description: Show the user a web page — usually a dev server you started in this container — in the workbench's side-by-side panel, a resizable pane next to the terminal; or turn that panel on/off. Use when the user asks to see/preview/show what a server renders, to enable/disable/toggle the side panel, split view, side-by-side view or preview pane, or asks to see a web page next to the terminal.
---

# Side-by-side panel

A pane beside the terminal that shows a URL in an iframe. Details are in the "Side-by-side panel" section of `/app/README.md`; what you need is below.

## The one thing to get right: the browser is not in this container

The iframe loads in the user's browser, on their machine. `http://localhost:3000` there is *their* laptop, never this container, so never point the panel at a localhost URL. Reach a port in the container through the workbench's own proxy instead, as a path on the workbench's origin:

- `/absproxy/<port>/` — the server sees the full path, prefix included, so it must be **told it lives under `/absproxy/<port>/`**. Absolute links and hot reload then work. Use this for anything with a base-path option, which is every real frontend.
- `/proxy/<port>/` — the prefix is stripped, so the server needs no configuration, but any *absolute* link it emits (`/app.js`) escapes the prefix and breaks. Use it only for simple servers with relative links, like `python -m http.server`.

Base-path flags for common tools (port 5173 as the example):

- Vite: `npx vite --port 5173 --strictPort --base /absproxy/5173/`
- Next.js: `basePath: '/absproxy/3000'` in `next.config.js`. Prefer passing it via an env var you add, rather than committing it.
- Anything else: look for "base path", "public path" or "root path" in its docs, and use the plain `/proxy/` route only if there isn't one.

Pick a port nothing else is using (`ss -ltn` lists the busy ones); other workspaces in this container may be running servers of their own. The server only has to listen on localhost.

## Showing it

1. Start the server in the background, with its base path set as above.
2. Point the panel at it, turning the panel on in the same call:

   ```bash
   curl -sS -X POST "http://127.0.0.1:${PORT:-5000}/api/ui/settings" \
     -H 'Content-Type: application/json' \
     -d '{"side_panel": true, "side_panel_url": "/absproxy/5173/"}'
   ```

   Open pages pick this up within a few seconds, with no reload, and the panel un-hides itself. While the server is still starting, the panel shows a page that keeps retrying, so the order of these two steps doesn't matter.
3. Before telling the user it's there, check it actually renders through the proxy, eg `curl -sS http://127.0.0.1:${PORT:-5000}/absproxy/5173/`. A 404 from the dev server usually means the base path doesn't match the URL.

`side_panel_url` also takes an `https://` URL (eg another openhost app), or `null` for the panel's home page. To turn the panel off, POST `{"side_panel": false}`. `GET` the same URL to read the current settings.

## Worth mentioning if it comes up

- Hiding the pane with `×` is per-browser and is not the same as turning the feature off. **◻ panel** in the top bar brings it back.
- Sites sending `X-Frame-Options: DENY` or a restrictive `frame-ancestors` won't render in the iframe. That's their choice and can't be worked around; the `↗` button opens the URL in a real tab instead, and that works for proxied URLs too.
- A proxied page runs on the workbench's own origin, so it can call the workbench API as the user. Fine for their own dev server, but don't proxy anything untrusted.
