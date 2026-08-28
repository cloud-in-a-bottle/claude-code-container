import { FitAddon } from '@xterm/addon-fit';
import { Terminal } from '@xterm/xterm';
import '@xterm/xterm/css/xterm.css';
import { Show, createEffect, createSignal, onCleanup, onMount } from 'solid-js';

import * as api from '../api';
import { filterOsc52Noise, handleOsc52 } from '../osc52';
import { forgetTab, state, theme } from '../store';
import { xtermTheme } from '../themes';

const RECONNECT_POLL_MS = 2000;

function wsUrl(tabId) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  return `${proto}://${location.host}/terminal/ws?tab=${encodeURIComponent(tabId)}`;
}

/** One terminal: an xterm instance bound to a server-side PTY over a websocket.
 *
 *  Deliberately imperative — xterm owns its own DOM and the websocket has a lifecycle of its own.
 *  Solid's job is to mount it, keep it in step with the colour scheme, and tear it down when the
 *  panel (or the whole workspace) goes away. Resizing is driven by dockview, which tells the panel
 *  its dimensions changed however that came about: a split, a drag, or the window itself. */
export function TerminalPane(props) {
  let host;
  let term;
  let fit;
  let socket = null;
  let reconnecting = false;
  let disposed = false;
  let lastDimensions = { cols: 80, rows: 24 };
  const [busy, setBusy] = createSignal('');

  function sendResize() {
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    const proposed = fit.proposeDimensions();
    if (proposed) lastDimensions = proposed;
    const { cols, rows } = proposed || lastDimensions;
    const json = new TextEncoder().encode(JSON.stringify({ type: 'resize', cols, rows }));
    const buf = new Uint8Array(1 + json.length);
    buf[0] = 0x01;
    buf.set(json, 1);
    socket.send(buf.buffer);
  }

  function refit() {
    // A hidden panel has no size to measure; it keeps the dimensions it last had.
    if (disposed || !props.panelApi.isVisible) return;
    fit.fit();
    sendResize();
  }

  /** After a dropped connection, work out whether the terminal is still there before reattaching. */
  function pollReconnect() {
    setTimeout(async () => {
      if (disposed || !reconnecting) return;
      try {
        const tabs = await api.listTabs(state.activeWorkspaceId);
        if (tabs.some((t) => t.id === props.tab.id)) connect();
        else forgetTab(props.tab.id);
      } catch (_) {
        pollReconnect(); // server not back yet
      }
    }, RECONNECT_POLL_MS);
  }

  function connect() {
    if (disposed) return;
    reconnecting = false;
    setBusy('');
    if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;

    const ws = new WebSocket(wsUrl(props.tab.id));
    ws.binaryType = 'arraybuffer';
    socket = ws;

    ws.onopen = () => {
      // Always send: a hidden panel reuses the dimensions it last knew about.
      if (props.panelApi.isVisible) fit.fit();
      sendResize();
    };
    ws.onmessage = (ev) => {
      if (!(ev.data instanceof ArrayBuffer)) {
        term.write(ev.data);
        return;
      }
      const bytes = new Uint8Array(ev.data);
      if (bytes[0] === 0x00) {
        const chunk = bytes.subarray(1);
        handleOsc52(chunk);
        term.write(filterOsc52Noise(chunk));
        return;
      }
      if (bytes[0] !== 0x01) return;
      try {
        const msg = JSON.parse(new TextDecoder().decode(bytes.subarray(1)));
        if (msg.type === 'busy') setBusy('This terminal is currently open in another browser tab.');
        else if (msg.type === 'kicked') setBusy('Another browser tab has taken over this terminal.');
        else if (msg.type === 'exit') term.write('\r\n\x1b[90m[process exited]\x1b[0m\r\n');
      } catch (_) {}
    };
    ws.onclose = () => {
      if (socket !== ws || disposed) return;
      socket = null;
      if (busy() || reconnecting) return;
      reconnecting = true;
      term.write('\r\n\x1b[90m[reconnecting...]\x1b[0m\r\n');
      pollReconnect();
    };
  }

  function copySelection() {
    const selection = term.getSelection();
    if (!selection) return false;
    navigator.clipboard.writeText(selection).catch(() => {
      const ta = document.createElement('textarea');
      ta.value = selection;
      ta.style.cssText = 'position:fixed;top:-9999px;opacity:0';
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand('copy');
      } catch (_) {}
      document.body.removeChild(ta);
    });
    return true;
  }

  onMount(() => {
    term = new Terminal({ cursorBlink: true, fontSize: 14, theme: xtermTheme(theme()) });
    fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host);
    term.attachCustomKeyEventHandler((e) => {
      if (e.type === 'keydown' && e.metaKey && e.key === 'c' && copySelection()) return false;
      return true;
    });
    term.onData((data) => {
      if (!socket || socket.readyState !== WebSocket.OPEN) return;
      const enc = new TextEncoder().encode(data);
      const buf = new Uint8Array(1 + enc.length);
      buf[0] = 0x00;
      buf.set(enc, 1);
      socket.send(buf.buffer);
    });
    connect();

    const sizeSub = props.panelApi.onDidDimensionsChange(() => refit());
    // Dockview detaches a hidden panel's DOM, so a panel coming back has to measure itself again.
    const visSub = props.panelApi.onDidVisibilityChange((e) => e.isVisible && queueMicrotask(refit));
    const focusSub = props.panelApi.onDidActiveChange((e) => e.isActive && term.focus());
    onCleanup(() => {
      sizeSub.dispose();
      visSub.dispose();
      focusSub.dispose();
    });
  });

  createEffect(() => {
    if (term) term.options.theme = xtermTheme(theme());
  });

  onCleanup(() => {
    disposed = true;
    reconnecting = false;
    if (socket) {
      socket.onclose = null;
      try {
        socket.close();
      } catch (_) {}
      socket = null;
    }
    try {
      term.dispose();
    } catch (_) {}
  });

  return (
    <div class="terminal-panel">
      <div class="term" ref={host} />
      <Show when={busy()}>
        <div class="busy-overlay">
          <div>{busy()}</div>
          <div class="busy-actions">
            <button class="busy-btn" type="button" onClick={() => forgetTab(props.tab.id)}>
              Close this panel
            </button>
            <button
              class="busy-btn"
              type="button"
              onClick={async () => {
                setBusy('');
                await api.kickTab(props.tab.id);
                connect();
              }}
            >
              Disconnect the other browser
            </button>
          </div>
        </div>
      </Show>
    </div>
  );
}
