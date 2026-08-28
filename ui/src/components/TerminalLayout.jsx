import { DockviewSolid } from '@arminmajerie/dockview-solid';
import '@arminmajerie/dockview-solid/styles/dockview.css';
import { Show, createEffect, onCleanup, untrack } from 'solid-js';

import { closeTab, newTab, state, takeFocusTab } from '../store';
import { TerminalPane } from './TerminalPane';

// A dockview theme is just a class name holding its CSS variables. Ours maps them onto the
// workbench palette in styles/dockview.css, so the layout follows the colour scheme picker without
// swapping themes here.
const WORKBENCH_THEME = { name: 'workbench', className: 'wb-dockview', dndPanelOverlay: 'group' };

const layoutKey = (workspaceId) => `workbench.layout.${workspaceId}`;
const SAVE_DEBOUNCE_MS = 400;

/** The terminal area: a dockview layout whose panels are the workspace's terminals.
 *
 *  The store stays the source of truth for *which* terminals exist; dockview owns where they sit.
 *  An effect reconciles the two, and the arrangement is saved per workspace so a reload comes back
 *  to the same split. */
export function TerminalLayout() {
  let dock;
  let mountedWorkspace = '';
  let saveTimer;
  // Set just before a new tab reaches the store, so the reconcile that follows can put its panel
  // in the group whose + was clicked rather than the default one.
  let pendingPlacement = null;
  // Panels we remove ourselves; their terminals are already gone, so closing must not kill twice.
  const removingSilently = new Set();

  function saveLayout() {
    if (!dock || !mountedWorkspace) return;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      try {
        localStorage.setItem(layoutKey(mountedWorkspace), JSON.stringify(dock.toJSON()));
      } catch (_) {
        /* a layout is not worth failing over */
      }
    }, SAVE_DEBOUNCE_MS);
  }

  function removeSilently(panel) {
    removingSilently.add(panel.id);
    dock.removePanel(panel);
  }

  function addPanel(tab, position) {
    dock.addPanel({
      id: tab.id,
      component: 'terminal',
      title: tab.label,
      params: { tabId: tab.id },
      ...(position ? { position } : {}),
    });
  }

  /** Bring dockview's panels in line with the workspace's tabs. */
  function reconcile() {
    const workspaceId = state.activeWorkspaceId;
    const tabs = state.tabs.map((t) => ({ id: t.id, label: t.label }));
    if (!dock) return;

    if (workspaceId !== mountedWorkspace) {
      for (const panel of [...dock.panels]) removeSilently(panel);
      mountedWorkspace = workspaceId;
      if (workspaceId) {
        try {
          const saved = localStorage.getItem(layoutKey(workspaceId));
          if (saved) dock.fromJSON(JSON.parse(saved));
        } catch (_) {
          // A layout that won't load isn't worth keeping.
          localStorage.removeItem(layoutKey(workspaceId));
        }
      }
    }

    // Panels for terminals that are gone — killed elsewhere, or not restored after a restart.
    for (const panel of [...dock.panels]) {
      if (!tabs.some((t) => t.id === panel.id)) removeSilently(panel);
    }
    for (const tab of tabs) {
      const existing = dock.getPanel(tab.id);
      if (existing) {
        if (existing.title !== tab.label) existing.api.setTitle(tab.label);
        continue;
      }
      const placement =
        pendingPlacement?.tabId === tab.id ? { referenceGroup: pendingPlacement.group } : undefined;
      if (placement) pendingPlacement = null;
      addPanel(tab, placement);
    }

    // A deep link (?tab=) or a reattached terminal asks to be brought to the front. Untracked:
    // taking it clears it, and this effect has no business re-running over that.
    const focusId = untrack(takeFocusTab);
    if (focusId) dock.getPanel(focusId)?.api.setActive();
  }

  /** Open a terminal, in `group` when one is given. */
  function addTerminal(group) {
    return newTab((created) => {
      if (group) pendingPlacement = { tabId: created.id, group };
    });
  }

  function onReady(event) {
    dock = event.api;
    event.api.onDidLayoutChange(saveLayout);
    event.api.onDidRemovePanel((panel) => {
      if (removingSilently.delete(panel.id)) return;
      // Closing a panel closes the terminal behind it: the process is killed and the tab leaves
      // the store. A Claude session survives it — the conversation is pinned to a session id, so a
      // new tab in the same workspace can resume it.
      closeTab(panel.id, true).catch(() => {});
    });
    reconcile();
  }

  createEffect(reconcile);

  onCleanup(() => {
    clearTimeout(saveTimer);
    // DockviewSolid disposes the dockview in its own cleanup; doing it here too would tear down
    // the panel portals twice.
    dock = undefined;
  });

  function TerminalPanel(panelProps) {
    const tabId = panelProps.params.tabId;
    const tab = () => state.tabs.find((t) => t.id === tabId);
    return (
      <Show when={tab()}>
        <TerminalPane tab={tab()} panelApi={panelProps.api} />
      </Show>
    );
  }

  function GroupActions(props) {
    return (
      <div class="dv-group-actions">
        <button type="button" title="New terminal in this group" onClick={() => addTerminal(props.group)}>
          +
        </button>
      </div>
    );
  }

  function Watermark() {
    return (
      <div class="dv-watermark">
        <Show
          when={state.activeWorkspaceId}
          fallback={
            <p>
              {state.projects.length
                ? 'Pick a workspace on the left, or create one with + next to a project.'
                : 'Add a project with + in the sidebar, then create a workspace in it.'}
            </p>
          }
        >
          <p>
            No terminals open here.{' '}
            <button class="wb-btn" type="button" onClick={() => addTerminal()}>
              Open one
            </button>
          </p>
        </Show>
      </div>
    );
  }

  return (
    <div id="terminal-layout">
      <DockviewSolid
        theme={WORKBENCH_THEME}
        components={{ terminal: TerminalPanel }}
        rightHeaderActionsComponent={GroupActions}
        watermarkComponent={Watermark}
        onReady={onReady}
      />
    </div>
  );
}
