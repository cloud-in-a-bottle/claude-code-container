import { DockviewSolid } from '@arminmajerie/dockview-solid';
import '@arminmajerie/dockview-solid/styles/dockview.css';
import { Show, createEffect, on, onCleanup, untrack } from 'solid-js';

import * as api from '../api';
import { closeTab, editorRequests, newTab, state, takeFocusTab } from '../store';
import { EditorPane } from './EditorPane';
import { TerminalPane } from './TerminalPane';

// A dockview theme is just a class name holding its CSS variables. Ours maps them onto the
// workbench palette in styles/dockview.css, so the layout follows the colour scheme picker without
// swapping themes here.
const WORKBENCH_THEME = { name: 'workbench', className: 'wb-dockview', dndPanelOverlay: 'group' };

const layoutKey = (workspaceId) => `workbench.layout.${workspaceId}`;
const SAVE_DEBOUNCE_MS = 400;

// Editor panels are keyed by the workspace they show, which both keeps them out of the terminal
// reconcile below and means a workspace can only ever have the one.
const EDITOR_PREFIX = 'editor:';
// dockview detaches a hidden panel's DOM by default, and re-attaching an <iframe> anywhere else in
// the document reloads it — so every switch between the terminal tab and the editor tab silently
// restarted VS Code, losing the cursor, unsaved buffers and any terminal running inside it. The
// 'always' renderer keeps the panel in one stable overlay container instead, which is what this
// option exists for. Terminals don't need it: xterm re-measures itself when it comes back.
const EDITOR_RENDERER = 'always';
const editorPanelId = (workspaceId) => EDITOR_PREFIX + workspaceId;
const isEditorPanel = (panelId) => panelId.startsWith(EDITOR_PREFIX);

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
    // Editor panels answer to nothing in the store, so they are not swept up here.
    for (const panel of [...dock.panels]) {
      if (isEditorPanel(panel.id)) {
        // A layout saved before EDITOR_RENDERER existed brings its editor back on the default one.
        if (panel.api.renderer !== EDITOR_RENDERER) panel.api.setRenderer(EDITOR_RENDERER);
        continue;
      }
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

  /** Show this workspace's editor, bringing the existing panel forward if it's already open. */
  function addEditor() {
    const workspaceId = state.activeWorkspaceId;
    if (!dock || !workspaceId) return;
    const existing = dock.getPanel(editorPanelId(workspaceId));
    if (existing) {
      existing.api.setActive();
      return;
    }
    dock.addPanel({
      id: editorPanelId(workspaceId),
      component: 'editor',
      title: 'editor',
      renderer: EDITOR_RENDERER,
      params: { workspaceId },
    });
  }

  function onReady(event) {
    dock = event.api;
    event.api.onDidLayoutChange(saveLayout);
    event.api.onDidRemovePanel((panel) => {
      if (removingSilently.delete(panel.id)) return;
      if (isEditorPanel(panel.id)) {
        // Closing the panel is as explicit as it gets, so the instance goes too rather than
        // holding its memory until the idle timeout notices. Reopening takes a few seconds and
        // comes back to the same files: the editor's state lives in its user-data-dir.
        api.stopEditor(panel.id.slice(EDITOR_PREFIX.length)).catch(() => {});
        return;
      }
      // Closing a panel closes the terminal behind it: the process is killed and the tab leaves
      // the store. A Claude session survives it — the conversation is pinned to a session id, so a
      // new tab in the same workspace can resume it.
      closeTab(panel.id, true).catch(() => {});
    });
    reconcile();
  }

  createEffect(reconcile);
  // defer: the count is non-zero only once something has actually asked.
  createEffect(on(editorRequests, addEditor, { defer: true }));

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

  function EditorPanel(panelProps) {
    return <EditorPane workspaceId={panelProps.params.workspaceId} />;
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
            Nothing open here.{' '}
            <button class="wb-btn" type="button" onClick={() => addTerminal()}>
              Open a terminal
            </button>{' '}
            <button class="wb-btn" type="button" onClick={addEditor}>
              Open the editor
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
        components={{ terminal: TerminalPanel, editor: EditorPanel }}
        rightHeaderActionsComponent={GroupActions}
        watermarkComponent={Watermark}
        onReady={onReady}
      />
    </div>
  );
}
