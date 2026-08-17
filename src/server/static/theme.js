/* Colour scheme picker.
 *
 * The chrome is themed by CSS variables in themes.css, keyed off data-theme on <html>, which the
 * server renders so there's no flash of the wrong colours on load. The terminal can't use those:
 * xterm.js renders to a canvas and wants its palette as a JS object, so the 16 ANSI colours live
 * here under the same theme names.
 *
 * Exposes window.workbenchTheme so app.js can colour terminals as it creates them and hand them
 * over to be restyled when the scheme changes. Loaded before app.js.
 */
(function () {
  'use strict';

  // Solarized uses one accent palette for both variants; only the base tones swap around.
  var SOLARIZED_ANSI = {
    black: '#073642', red: '#dc322f', green: '#859900', yellow: '#b58900',
    blue: '#268bd2', magenta: '#d33682', cyan: '#2aa198', white: '#eee8d5',
    brightBlack: '#002b36', brightRed: '#cb4b16', brightGreen: '#586e75',
    brightYellow: '#657b83', brightBlue: '#839496', brightMagenta: '#6c71c4',
    brightCyan: '#93a1a1', brightWhite: '#fdf6e3'
  };

  function withBase(ansi, base) {
    var out = {};
    for (var k in ansi) if (Object.prototype.hasOwnProperty.call(ansi, k)) out[k] = ansi[k];
    for (var j in base) if (Object.prototype.hasOwnProperty.call(base, j)) out[j] = base[j];
    return out;
  }

  // Keep these names in sync with THEMES in ui_settings.py.
  var THEMES = {
    'dark': {
      label: 'Dark',
      xterm: { background: '#1e1e1e', foreground: '#dddddd', cursor: '#dddddd' }
    },
    'solarized-light': {
      label: 'Solarized Light',
      xterm: withBase(SOLARIZED_ANSI, {
        background: '#fdf6e3', foreground: '#657b83',
        cursor: '#586e75', cursorAccent: '#fdf6e3',
        selectionBackground: '#eee8d5', selectionForeground: '#586e75'
      })
    },
    'solarized-dark': {
      label: 'Solarized Dark',
      xterm: withBase(SOLARIZED_ANSI, {
        background: '#002b36', foreground: '#839496',
        cursor: '#93a1a1', cursorAccent: '#002b36',
        selectionBackground: '#073642', selectionForeground: '#93a1a1'
      })
    }
  };

  var DEFAULT_THEME = 'dark';
  var terminals = [];
  var current = document.documentElement.getAttribute('data-theme') || DEFAULT_THEME;
  if (!THEMES[current]) current = DEFAULT_THEME;

  function xtermTheme() {
    return THEMES[current].xterm;
  }

  /** Register a terminal so it gets restyled when the scheme changes. */
  function register(term) {
    terminals.push(term);
  }

  function paintFrame() {
    // The side panel is a separate document. Same-origin frames can be restyled directly;
    // anything cross-origin throws on access, and isn't ours to touch anyway.
    var frame = document.getElementById('sp-frame');
    if (!frame) return;
    try {
      var doc = frame.contentDocument;
      if (doc && doc.documentElement) doc.documentElement.setAttribute('data-theme', current);
    } catch (_) { /* cross-origin */ }
  }

  function apply(name, persist) {
    if (!THEMES[name]) return;
    current = name;
    document.documentElement.setAttribute('data-theme', name);

    var theme = xtermTheme();
    for (var i = 0; i < terminals.length; i++) {
      // xterm 5 exposes a settable options object; older builds used setOption().
      try {
        if (terminals[i].options) terminals[i].options.theme = theme;
        else if (terminals[i].setOption) terminals[i].setOption('theme', theme);
      } catch (_) {}
    }
    paintFrame();

    if (persist === false) return;
    fetch('/api/ui/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ theme: name })
    }).catch(function () {
      // The scheme is already applied locally; it just won't outlive this page.
      console.warn('could not save the colour scheme');
    });
  }

  function buildPicker() {
    var host = document.getElementById('tab-actions') || document.getElementById('tabs');
    if (!host) return;

    var select = document.createElement('select');
    select.id = 'theme-picker';
    select.title = 'Colour scheme';
    select.setAttribute('aria-label', 'Colour scheme');
    for (var name in THEMES) {
      if (!Object.prototype.hasOwnProperty.call(THEMES, name)) continue;
      var opt = document.createElement('option');
      opt.value = name;
      opt.textContent = THEMES[name].label;
      if (name === current) opt.selected = true;
      select.appendChild(opt);
    }
    select.addEventListener('change', function () { apply(select.value, true); });
    host.appendChild(select);
  }

  window.workbenchTheme = {
    xterm: xtermTheme,
    register: register,
    apply: apply,
    current: function () { return current; },
    names: function () { return Object.keys(THEMES); }
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', buildPicker);
  } else {
    buildPicker();
  }
})();
