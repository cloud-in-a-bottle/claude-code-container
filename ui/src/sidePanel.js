import { createSignal } from 'solid-js';

import * as api from './api';

export const MIN_SIDE = 240;
export const MIN_LEFT = 320;
export const DEFAULT_SIDE = 460;
export const HOME_URL = '/static/side-panel-home.html';

const WIDTH_KEY = 'workbench.sidePanel.width';
const HIDDEN_KEY = 'workbench.sidePanel.hidden';

/** How often an open page re-reads whether the panel is on and what it shows. Both are set from a
 *  terminal (the /side-by-side skill), so nothing else would tell the page they changed. */
const SETTINGS_POLL_MS = 3000;

const bootstrap = window.__WORKBENCH__ || {};

// Per-browser, unlike the server-side settings below: where you like the divider is a property of
// the screen you're sitting at.
const [width, setWidthSignal] = createSignal(Number(localStorage.getItem(WIDTH_KEY)) || DEFAULT_SIDE);
const [hidden, setHiddenSignal] = createSignal(localStorage.getItem(HIDDEN_KEY) === '1');

// Server-side, so Claude can turn the panel on and point it at a dev server it just started.
const [enabled, setEnabled] = createSignal(!!bootstrap.side_panel);
const [url, setUrlSignal] = createSignal(bootstrap.side_panel_url || HOME_URL);

export { width, hidden, url, enabled };

export function setWidth(px) {
  const clamped = Math.max(MIN_SIDE, Math.min(px, window.innerWidth - MIN_LEFT));
  setWidthSignal(clamped);
  localStorage.setItem(WIDTH_KEY, String(clamped));
}

export function setHidden(value) {
  setHiddenSignal(value);
  localStorage.setItem(HIDDEN_KEY, value ? '1' : '0');
}

/** The URL last seen on the server, so a poll only navigates when someone else changed it. */
let serverUrl = bootstrap.side_panel_url ?? null;

/** Navigate from the URL bar. Saved server-side too, or the next poll would put the old one back. */
export function setUrl(value) {
  setUrlSignal(value);
  serverUrl = value;
  api.saveUiSettings({ side_panel_url: value }).catch((err) => {
    // Already showing; it just won't be what the next page load opens on.
    console.warn('could not save the side panel URL', err);
  });
}

function apply(settings) {
  if (settings.side_panel && !enabled()) setHidden(false);
  setEnabled(!!settings.side_panel);
  const next = settings.side_panel_url ?? null;
  if (next === serverUrl) return;
  serverUrl = next;
  setUrlSignal(next || HOME_URL);
  // Somebody pointed the panel at something on purpose, so show it.
  setHidden(false);
}

/** Follow the server-side settings while the page is being looked at. A failed poll keeps the panel
 *  as it is; the next one tries again. */
export function watchPanelSettings() {
  const tick = () => {
    if (document.visibilityState !== 'visible') return;
    api
      .getUiSettings()
      .then(apply)
      .catch(() => {});
  };
  setInterval(tick, SETTINGS_POLL_MS);
  document.addEventListener('visibilitychange', tick);
}
