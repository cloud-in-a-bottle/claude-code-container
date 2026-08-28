import { createSignal } from 'solid-js';

export const MIN_SIDE = 240;
export const MIN_LEFT = 320;
export const DEFAULT_SIDE = 460;
export const HOME_URL = '/static/side-panel-home.html';

const WIDTH_KEY = 'workbench.sidePanel.width';
const HIDDEN_KEY = 'workbench.sidePanel.hidden';
const URL_KEY = 'workbench.sidePanel.url';

// Per-browser, unlike the server-side on/off setting: where you like the divider is a property of
// the screen you're sitting at.
const [width, setWidthSignal] = createSignal(Number(localStorage.getItem(WIDTH_KEY)) || DEFAULT_SIDE);
const [hidden, setHiddenSignal] = createSignal(localStorage.getItem(HIDDEN_KEY) === '1');
const [url, setUrlSignal] = createSignal(localStorage.getItem(URL_KEY) || HOME_URL);

export { width, hidden, url };

export function setWidth(px) {
  const clamped = Math.max(MIN_SIDE, Math.min(px, window.innerWidth - MIN_LEFT));
  setWidthSignal(clamped);
  localStorage.setItem(WIDTH_KEY, String(clamped));
}

export function setHidden(value) {
  setHiddenSignal(value);
  localStorage.setItem(HIDDEN_KEY, value ? '1' : '0');
}

export function setUrl(value) {
  setUrlSignal(value);
  localStorage.setItem(URL_KEY, value);
}

export const sidePanelEnabled = !!(window.__WORKBENCH__ || {}).side_panel;
