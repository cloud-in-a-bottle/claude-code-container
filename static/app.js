(function () {
  const tabsEl = document.getElementById('tabs');
  const newTabEl = document.getElementById('new-tab');
  const panesEl = document.getElementById('panes');
  const tabs = [];
  let activeMenu = null;
  let activeEntry = null;

  function wsUrl(tabId) {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    return `${proto}://${location.host}/terminal/ws?tab=${encodeURIComponent(tabId)}`;
  }

  function sendResize(entry) {
    if (!entry.ws || entry.ws.readyState !== WebSocket.OPEN) return;
    const d = entry.fit.proposeDimensions();
    if (!d) return;
    const json = new TextEncoder().encode(JSON.stringify({ type: 'resize', cols: d.cols, rows: d.rows }));
    const buf = new Uint8Array(1 + json.length);
    buf[0] = 0x01; buf.set(json, 1);
    entry.ws.send(buf.buffer);
  }

  function pollReconnect(entry) {
    setTimeout(async () => {
      if (!entry.reconnecting) return;
      try {
        const resp = await fetch('/api/tabs');
        if (!resp.ok) { pollReconnect(entry); return; }
        const serverTabs = await resp.json();
        if (serverTabs.some(t => t.id === entry.serverId)) {
          connectTab(entry); // tab still exists — network hiccup
        } else {
          location.reload(); // server restarted with new tabs
        }
      } catch (_) {
        pollReconnect(entry); // server not ready yet
      }
    }, 2000);
  }

  function connectTab(entry) {
    entry.reconnecting = false;
    removeBusyOverlay(entry);
    if (entry.ws && (entry.ws.readyState === WebSocket.OPEN || entry.ws.readyState === WebSocket.CONNECTING)) return;

    const ws = new WebSocket(wsUrl(entry.serverId));
    ws.binaryType = 'arraybuffer';
    entry.ws = ws;

    ws.onopen = () => { if (activeEntry === entry) { entry.fit.fit(); sendResize(entry); } };
    ws.onmessage = (ev) => {
      if (!(ev.data instanceof ArrayBuffer)) { entry.term.write(ev.data); return; }
      const bytes = new Uint8Array(ev.data);
      if (bytes[0] === 0x00) {
        entry.term.write(bytes.subarray(1));
      } else if (bytes[0] === 0x01) {
        try {
          const msg = JSON.parse(new TextDecoder().decode(bytes.subarray(1)));
          if (msg.type === 'busy') showBusyOverlay(entry);
          else if (msg.type === 'kicked') showBusyOverlay(entry, 'Another tab has taken over this terminal.');
          else if (msg.type === 'exit') entry.term.write('\r\n\x1b[90m[process exited]\x1b[0m\r\n');
        } catch (_) {}
      }
    };
    ws.onclose = () => {
      if (entry.ws !== ws) return;
      if (entry.busyOverlay) return;
      entry.ws = null;
      if (!entry.reconnecting) {
        entry.reconnecting = true;
        entry.term.write('\r\n\x1b[90m[reconnecting...]\x1b[0m\r\n');
        pollReconnect(entry);
      }
    };
  }

  function disconnectTab(entry) {
    entry.reconnecting = false;
    if (!entry.ws) return;
    const ws = entry.ws;
    entry.ws = null;
    ws.onclose = null;
    try { ws.close(); } catch (_) {}
  }

  function showBusyOverlay(entry, msg = 'This terminal is currently open in another tab.') {
    removeBusyOverlay(entry);
    const overlay = document.createElement('div');
    overlay.className = 'busy-overlay';
    const safeMsg = document.createTextNode(msg);
    overlay.innerHTML = `
      <div></div>
      <div class="busy-actions">
        <button class="busy-btn" data-action="close">Close this tab</button>
        <button class="busy-btn" data-action="kick">Disconnect other tab</button>
      </div>`;
    overlay.querySelector('div').appendChild(safeMsg);
    overlay.querySelector('[data-action="close"]').addEventListener('click', () => detachClientTab(entry));
    overlay.querySelector('[data-action="kick"]').addEventListener('click', async () => {
      removeBusyOverlay(entry);
      await fetch('/api/tabs/' + entry.serverId + '/kick', { method: 'POST' });
      connectTab(entry);
    });
    entry.paneEl.appendChild(overlay);
    entry.busyOverlay = overlay;
  }

  function removeBusyOverlay(entry) {
    if (entry.busyOverlay) { entry.busyOverlay.remove(); entry.busyOverlay = null; }
  }

  function openClientTab(serverId, label, autoActivate = true) {
    const tabEl = document.createElement('div');
    tabEl.className = 'tab';
    tabEl.innerHTML = `<span class="label"></span><span class="close" title="Close">×</span>`;
    tabEl.querySelector('.label').textContent = label;
    tabsEl.insertBefore(tabEl, newTabEl);

    const paneEl = document.createElement('div');
    paneEl.className = 'pane';
    const termEl = document.createElement('div');
    termEl.className = 'term';
    paneEl.appendChild(termEl);
    panesEl.appendChild(paneEl);

    const term = new Terminal({ cursorBlink: true, fontSize: 14,
      theme: { background: '#1e1e1e' }, allowProposedApi: true });
    const fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    term.loadAddon(new ClipboardAddon.ClipboardAddon());
    term.open(termEl);

    term.attachCustomKeyEventHandler((e) => {
      if (e.type === 'keydown' && e.metaKey && e.key === 'c') {
        const sel = term.getSelection();
        if (sel) { navigator.clipboard.writeText(sel); return false; }
      }
      return true;
    });

    const entry = { serverId, label, tabEl, paneEl, term, fit, ws: null, busyOverlay: null, reconnecting: false };
    tabs.push(entry);

    term.onData((data) => {
      if (!entry.ws || entry.ws.readyState !== WebSocket.OPEN) return;
      const enc = new TextEncoder().encode(data);
      const buf = new Uint8Array(1 + enc.length);
      buf[0] = 0x00; buf.set(enc, 1);
      entry.ws.send(buf.buffer);
    });

    tabEl.addEventListener('click', (e) => {
      if (e.target.classList.contains('close')) return;
      activate(entry);
    });

    const closeBtn = tabEl.querySelector('.close');
    closeBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      closeMenu();
      showMenu(closeBtn, [
        { label: 'Close client tab', action: () => detachClientTab(entry) },
        { label: 'Delete server tab', action: async () => {
          await fetch('/api/tabs/' + serverId, { method: 'DELETE' });
          detachClientTab(entry);
        }},
      ]);
    });

    if (autoActivate) activate(entry);
    return entry;
  }

  function activate(entry) {
    activeEntry = entry;
    for (const t of tabs) {
      t.tabEl.classList.toggle('active', t === entry);
      t.paneEl.classList.toggle('active', t === entry);
    }
    connectTab(entry);
    setTimeout(() => { entry.fit.fit(); sendResize(entry); }, 0);
  }

  function detachClientTab(entry) {
    disconnectTab(entry);
    removeBusyOverlay(entry);
    try { entry.term.dispose(); } catch (_) {}
    entry.tabEl.remove();
    entry.paneEl.remove();
    const idx = tabs.indexOf(entry);
    if (idx >= 0) tabs.splice(idx, 1);
    if (activeEntry === entry) activeEntry = null;
    if (tabs.length === 0) createNewServerTab();
    else activate(tabs[Math.min(idx, tabs.length - 1)]);
  }

  async function createNewServerTab(label) {
    const body = label ? JSON.stringify({ label }) : '{}';
    const resp = await fetch('/api/tabs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body,
    });
    const data = await resp.json();
    return openClientTab(data.id, data.label);
  }

  function showMenu(anchorEl, items) {
    closeMenu();
    const menu = document.createElement('div');
    menu.className = 'tab-menu';
    for (const item of items) {
      if (item === null) {
        const sep = document.createElement('div');
        sep.className = 'tab-menu-sep';
        menu.appendChild(sep);
      } else {
        const el = document.createElement('div');
        el.className = 'tab-menu-item' + (item.disabled ? ' disabled' : '');
        el.textContent = item.label;
        if (!item.disabled) {
          el.addEventListener('click', () => { closeMenu(); item.action(); });
        }
        menu.appendChild(el);
      }
    }
    document.body.appendChild(menu);
    activeMenu = menu;

    const rect = anchorEl.getBoundingClientRect();
    menu.style.left = rect.left + 'px';
    menu.style.top = rect.bottom + 'px';

    requestAnimationFrame(() => {
      document.addEventListener('click', closeMenu, { once: true, capture: true });
    });
  }

  function closeMenu() {
    if (activeMenu) {
      activeMenu.remove();
      activeMenu = null;
    }
  }

  newTabEl.addEventListener('click', async (e) => {
    e.stopPropagation();
    closeMenu();

    const resp = await fetch('/api/tabs');
    const serverTabs = await resp.json();
    const openIds = new Set(tabs.map(t => t.serverId));
    const existing = serverTabs.filter(st => !openIds.has(st.id));

    if (existing.length === 0) {
      createNewServerTab();
      return;
    }

    const items = existing.map(st => ({
      label: st.label + (st.connected ? ' [busy]' : (!st.alive ? ' [exited]' : '')),
      action: () => openClientTab(st.id, st.label),
    }));

    items.push(null);
    items.push({ label: '+ New terminal', action: () => createNewServerTab() });

    showMenu(newTabEl, items);
  });

  window.addEventListener('resize', () => {
    if (activeEntry) activeEntry.fit.fit();
  });

  async function init() {
    const params = new URLSearchParams(location.search);
    const tabId = params.get('tab');
    if (tabId) history.replaceState({}, '', location.pathname);

    const resp = await fetch('/api/tabs');
    const serverTabs = await resp.json();

    if (serverTabs.length === 0) {
      await createNewServerTab();
    } else {
      for (const st of serverTabs) {
        openClientTab(st.id, st.label, false); // create UI without activating
      }
      const toActivate = tabId
        ? (tabs.find(t => t.serverId === tabId) || tabs[0])
        : tabs[0];
      // Connect background tabs immediately to claim their locks.
      // The active tab is connected by activate() so the terminal is sized
      // before ws.onopen fires and the server sends the output buffer.
      for (const t of tabs) {
        if (t !== toActivate) connectTab(t);
      }
      activate(toActivate);
    }
  }

  init();
})();
