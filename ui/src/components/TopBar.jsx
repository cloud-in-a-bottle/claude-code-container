import { Show, createSignal } from 'solid-js';

import * as api from '../api';
import { hidden as panelHidden, setHidden as setPanelHidden, sidePanelEnabled } from '../sidePanel';
import { activeWorkspace, adoptTab, newTab, requestEditor, sidebarHidden, state, toggleSidebar } from '../store';
import { Menu } from './Menu';
import { ThemePicker } from './ThemePicker';

export function TopBar() {
  const [menu, setMenu] = createSignal(null);
  const current = () => activeWorkspace();

  /** Terminals the server still has for this workspace that aren't on the page — one closed in the
   *  browser without being killed, or one restored after a restart. */
  async function openTerminalMenu(anchor) {
    const detached = (await api.listTabs(state.activeWorkspaceId)).filter(
      (t) => !state.tabs.some((open) => open.id === t.id),
    );
    if (detached.length === 0) {
      newTab();
      return;
    }
    setMenu({
      anchor,
      items: [
        ...detached.map((t) => ({
          label: t.label + (t.connected ? ' [busy]' : t.alive ? '' : ' [exited]'),
          description: [t.cwd, t.program].filter(Boolean).join(' · '),
          action: () => adoptTab(t),
        })),
        null,
        { label: '+ New terminal', action: () => newTab() },
      ],
    });
  }

  return (
    <div id="topbar">
      <Show when={sidebarHidden()}>
        <div id="sidebar-toggle" title="Show projects" onClick={toggleSidebar}>
          &#9776;
        </div>
      </Show>
      <Show when={current()} fallback={<div id="workspace-label">no workspace open</div>}>
        <div id="workspace-label" title={current().workspace.path}>
          {current().project.name} <span class="sep">/</span> <b>{current().workspace.name}</b>
        </div>
      </Show>
      <Show when={state.activeWorkspaceId}>
        <button id="new-terminal" type="button" title="New terminal" onClick={(e) => openTerminalMenu(e.currentTarget)}>
          + terminal
        </button>
        <button class="bar-btn" type="button" title="Open this workspace in VS Code" onClick={requestEditor}>
          + editor
        </button>
      </Show>
      <div id="topbar-actions">
        <Show when={sidePanelEnabled && panelHidden()}>
          <button class="bar-btn" type="button" title="Show the side panel" onClick={() => setPanelHidden(false)}>
            &#9723; panel
          </button>
        </Show>
        <ThemePicker />
      </div>
      <Show when={menu()}>
        <Menu anchor={menu().anchor} items={menu().items} onClose={() => setMenu(null)} />
      </Show>
    </div>
  );
}
