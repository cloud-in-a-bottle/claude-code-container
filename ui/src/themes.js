/* The terminal half of a colour scheme.
 *
 * The chrome is themed by CSS variables in static/themes.css, keyed off data-theme on <html>,
 * which the server renders so there's no flash of the wrong colours on load. xterm.js renders to a
 * canvas and wants its palette as a JS object, so the 16 ANSI colours live here under the same
 * theme names. Keep these names in sync with THEMES in ui_settings.py — a test checks they agree.
 */

// Solarized uses one accent palette for both variants; only the base tones swap around.
const SOLARIZED_ANSI = {
  black: '#073642', red: '#dc322f', green: '#859900', yellow: '#b58900',
  blue: '#268bd2', magenta: '#d33682', cyan: '#2aa198', white: '#eee8d5',
  brightBlack: '#002b36', brightRed: '#cb4b16', brightGreen: '#586e75',
  brightYellow: '#657b83', brightBlue: '#839496', brightMagenta: '#6c71c4',
  brightCyan: '#93a1a1', brightWhite: '#fdf6e3',
};

export const THEMES = {
  'solarized-light': {
    label: 'Solarized Light',
    xterm: {
      ...SOLARIZED_ANSI,
      background: '#fdf6e3', foreground: '#657b83',
      cursor: '#586e75', cursorAccent: '#fdf6e3',
      selectionBackground: '#eee8d5', selectionForeground: '#586e75',
    },
  },
  'solarized-dark': {
    label: 'Solarized Dark',
    xterm: {
      ...SOLARIZED_ANSI,
      background: '#002b36', foreground: '#839496',
      cursor: '#93a1a1', cursorAccent: '#002b36',
      selectionBackground: '#073642', selectionForeground: '#93a1a1',
    },
  },
  'dark': {
    label: 'Dark',
    xterm: { background: '#1e1e1e', foreground: '#dddddd', cursor: '#dddddd' },
  },
};

export const DEFAULT_THEME = 'solarized-light';

export const xtermTheme = (name) => (THEMES[name] || THEMES[DEFAULT_THEME]).xterm;
