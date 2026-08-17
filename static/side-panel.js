/* Side-by-side panel: splits the window between the terminal and an iframe.
 *
 * Loaded from index.html only when the side_panel UI setting is on, so this file is inert
 * (and never fetched) by default. It injects its own CSS and builds its own DOM at runtime,
 * which keeps the footprint in index.html to a single conditional <script> tag.
 *
 * Terminal resizing deliberately goes through a synthetic window 'resize' event rather than
 * calling into app.js: xterm needs an explicit refit whenever its pane changes width, and
 * app.js already listens for exactly that.
 */
(function () {
  'use strict';

  var MIN_SIDE = 240, MIN_LEFT = 320, DEFAULT_SIDE = 460, HOME_URL = '/static/side-panel-home.html';

  var panes = document.getElementById('panes');
  var tabsBar = document.getElementById('tabs');
  if (!panes || !tabsBar) return; // markup changed out from under us; leave the UI alone

  // ---- styles ---------------------------------------------------------------
  var css = [
    '#sp-toggle { margin-left: auto; align-self: center; padding: 4px 12px; cursor: pointer;',
    '  color: #888; font-size: 12px; white-space: nowrap; }',
    '#sp-toggle:hover { color: #fff; }',
    '#sp-split { flex: 1; display: flex; min-height: 0; }',
    '#sp-left { flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; }',
    '#sp-divider { flex: 0 0 6px; background: #111; border-left: 1px solid #333;',
    '  border-right: 1px solid #333; cursor: col-resize; }',
    '#sp-divider:hover, #sp-divider.dragging { background: #3a3a3a; }',
    '#sp-divider:focus-visible { outline: 2px solid #7fd1b9; outline-offset: -2px; }',
    '#sp-side { flex: 0 0 460px; min-width: 0; display: flex; flex-direction: column; background: #1e1e1e; }',
    '#sp-bar { display: flex; align-items: center; gap: 4px; padding: 4px 6px;',
    '  background: #111; border-bottom: 1px solid #333; }',
    '#sp-url { flex: 1; min-width: 0; background: #1e1e1e; border: 1px solid #333; color: #ddd;',
    '  border-radius: 3px; padding: 3px 7px; font-size: 12px; font-family: ui-monospace, Menlo, monospace; }',
    '#sp-url:focus { outline: none; border-color: #666; }',
    '.sp-btn { background: none; border: 1px solid transparent; color: #888; cursor: pointer;',
    '  font-size: 13px; padding: 2px 7px; border-radius: 3px; line-height: 1.4; }',
    '.sp-btn:hover { color: #fff; background: #2a2a2a; border-color: #444; }',
    '#sp-frame { flex: 1; width: 100%; border: 0; background: #1e1e1e; }',
    'body.sp-hidden #sp-side, body.sp-hidden #sp-divider { display: none; }',
    'body.sp-dragging { user-select: none; }',
    'body.sp-dragging #sp-frame { pointer-events: none; }'
  ].join('\n');
  var styleEl = document.createElement('style');
  styleEl.id = 'sp-style';
  styleEl.textContent = css;
  document.head.appendChild(styleEl);

  // ---- build the DOM around the existing #panes -----------------------------
  // #panes is moved, not recreated, so the reference app.js already cached stays valid.
  var split = document.createElement('div');
  split.id = 'sp-split';
  var left = document.createElement('div');
  left.id = 'sp-left';
  var divider = document.createElement('div');
  divider.id = 'sp-divider';
  divider.setAttribute('role', 'separator');
  divider.setAttribute('aria-orientation', 'vertical');
  divider.setAttribute('aria-label', 'Resize side panel');
  divider.tabIndex = 0;

  var side = document.createElement('aside');
  side.id = 'sp-side';
  side.innerHTML =
    '<div id="sp-bar">' +
      '<input id="sp-url" spellcheck="false" aria-label="Side panel URL">' +
      '<button class="sp-btn" id="sp-reload" type="button" title="Reload">&#8635;</button>' +
      '<button class="sp-btn" id="sp-pop" type="button" title="Open in new tab">&#8599;</button>' +
      '<button class="sp-btn" id="sp-close" type="button" title="Hide panel">&times;</button>' +
    '</div>' +
    '<iframe id="sp-frame" title="Side panel"></iframe>';

  panes.parentNode.insertBefore(split, panes);
  left.appendChild(panes);
  split.appendChild(left);
  split.appendChild(divider);
  split.appendChild(side);

  var toggle = document.createElement('div');
  toggle.id = 'sp-toggle';
  toggle.title = 'Show/hide the side panel';
  toggle.textContent = '◻ panel';
  tabsBar.appendChild(toggle);

  var body = document.body;
  var frame = document.getElementById('sp-frame');
  var urlInput = document.getElementById('sp-url');

  // ---- state ----------------------------------------------------------------
  function store(k, v) { try { localStorage.setItem(k, v); } catch (_) {} }
  function load(k) { try { return localStorage.getItem(k); } catch (_) { return null; } }

  var pending = false;
  function refit() {
    if (pending) return;
    pending = true;
    requestAnimationFrame(function () {
      pending = false;
      window.dispatchEvent(new Event('resize'));
    });
  }

  function clampWidth(px) {
    var max = Math.max(MIN_SIDE, window.innerWidth - MIN_LEFT);
    return Math.round(Math.min(Math.max(px, MIN_SIDE), max));
  }

  function setWidth(px, persist) {
    var w = clampWidth(px);
    side.style.flexBasis = w + 'px';
    if (persist !== false) store('sidePanel.width', String(w));
    refit();
  }

  function setHidden(hidden) {
    body.classList.toggle('sp-hidden', hidden);
    store('sidePanel.hidden', hidden ? '1' : '0');
    refit();
  }

  var savedWidth = parseInt(load('sidePanel.width') || '', 10);
  setWidth(isNaN(savedWidth) ? DEFAULT_SIDE : savedWidth, false);
  if (load('sidePanel.hidden') === '1') body.classList.add('sp-hidden');
  var startUrl = load('sidePanel.url') || HOME_URL;
  urlInput.value = startUrl;
  frame.src = startUrl;

  // ---- drag to resize -------------------------------------------------------
  function onMove(e) {
    var x = e.touches ? e.touches[0].clientX : e.clientX;
    setWidth(window.innerWidth - x - 3);
  }
  function onUp() {
    body.classList.remove('sp-dragging');
    divider.classList.remove('dragging');
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    document.removeEventListener('touchmove', onMove);
    document.removeEventListener('touchend', onUp);
    refit();
  }
  function onDown(e) {
    e.preventDefault();
    body.classList.add('sp-dragging');
    divider.classList.add('dragging');
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
    document.addEventListener('touchmove', onMove, { passive: true });
    document.addEventListener('touchend', onUp);
  }
  divider.addEventListener('mousedown', onDown);
  divider.addEventListener('touchstart', onDown);
  divider.addEventListener('dblclick', function () { setWidth(DEFAULT_SIDE); });
  divider.addEventListener('keydown', function (e) {
    var step = e.shiftKey ? 60 : 20;
    if (e.key === 'ArrowLeft') { setWidth(side.getBoundingClientRect().width + step); e.preventDefault(); }
    else if (e.key === 'ArrowRight') { setWidth(side.getBoundingClientRect().width - step); e.preventDefault(); }
  });

  // ---- panel controls -------------------------------------------------------
  function navigate() {
    var v = urlInput.value.trim();
    if (!v) return;
    store('sidePanel.url', v);
    frame.src = v;
  }
  urlInput.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { navigate(); frame.focus(); }
  });
  document.getElementById('sp-reload').addEventListener('click', function () {
    frame.src = frame.src; // reassigning reloads even when the URL is unchanged
  });
  document.getElementById('sp-pop').addEventListener('click', function () {
    window.open(urlInput.value.trim() || HOME_URL, '_blank', 'noopener');
  });
  document.getElementById('sp-close').addEventListener('click', function () { setHidden(true); });
  toggle.addEventListener('click', function () {
    setHidden(!body.classList.contains('sp-hidden'));
  });

  window.addEventListener('resize', function () {
    var w = side.getBoundingClientRect().width;
    if (!w) return;
    var clamped = clampWidth(w);
    if (clamped !== Math.round(w)) side.style.flexBasis = clamped + 'px';
  });
})();
