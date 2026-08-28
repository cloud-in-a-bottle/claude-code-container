import { batch, createSignal } from 'solid-js';
import { createStore } from 'solid-js/store';

import * as api from './api';
import { DEFAULT_THEME, THEMES } from './themes';

const bootstrap = window.__WORKBENCH__ || {};

const LAST_WORKSPACE_KEY = 'workbench.workspace';
const SIDEBAR_HIDDEN_KEY = 'workbench.sidebarHidden';

const [state, setState] = createStore({
  projects: [],
  /** Terminals of the active workspace only — switching workspaces replaces this wholesale.
   *  Which of them is visible, and how they're arranged, is dockview's business, not the store's. */
  tabs: [],
  activeWorkspaceId: '',
  /** A terminal the layout should bring to the front once its panel exists. Cleared when used. */
  focusTabId: '',
  ready: false,
});

const [theme, setThemeSignal] = createSignal(THEMES[bootstrap.theme] ? bootstrap.theme : DEFAULT_THEME);
/** Bumped when something asks for an editor panel. The layout owns dockview, so it does the
 *  opening; this is how the top bar reaches it without a reference to the dock. */
const [editorRequests, setEditorRequests] = createSignal(0);
const [sidebarHidden, setSidebarHiddenSignal] = createSignal(localStorage.getItem(SIDEBAR_HIDDEN_KEY) === '1');

/** Guards against a slow workspace load landing after the user has moved on to another one. */
let switchToken = 0;

export { state, theme, sidebarHidden, editorRequests };

export function requestEditor() {
  setEditorRequests((n) => n + 1);
}

export function toggleSidebar() {
  const hidden = !sidebarHidden();
  setSidebarHiddenSignal(hidden);
  localStorage.setItem(SIDEBAR_HIDDEN_KEY, hidden ? '1' : '0');
}

export function applyTheme(name) {
  if (!THEMES[name]) return;
  setThemeSignal(name);
  document.documentElement.setAttribute('data-theme', name);
  api.saveUiSettings({ theme: name }).catch(() => {
    // Already applied locally; it just won't outlive this page.
    console.warn('could not save the colour scheme');
  });
}

export function activeWorkspace() {
  for (const project of state.projects) {
    const found = project.workspaces.find((w) => w.id === state.activeWorkspaceId);
    if (found) return { project, workspace: found };
  }
  return null;
}

export function takeFocusTab() {
  const id = state.focusTabId;
  if (id) setState('focusTabId', '');
  return id;
}

export async function refreshProjects() {
  setState('projects', await api.listProjects());
}

function rememberWorkspace(workspaceId) {
  localStorage.setItem(LAST_WORKSPACE_KEY, workspaceId);
  const url = new URL(location.href);
  url.searchParams.set('workspace', workspaceId);
  url.searchParams.delete('tab');
  history.replaceState({}, '', url);
}

/** Show a workspace: swap in its terminals, opening one if it has none yet. */
export async function openWorkspace(workspaceId) {
  if (workspaceId === state.activeWorkspaceId) return;
  const token = ++switchToken;

  batch(() => {
    setState('activeWorkspaceId', workspaceId);
    setState('tabs', []);
  });
  rememberWorkspace(workspaceId);

  let tabs = await api.listTabs(workspaceId);
  if (token !== switchToken) return;
  if (tabs.length === 0) {
    tabs = [await api.createTab(workspaceId)];
    if (token !== switchToken) return;
  }
  setState('tabs', tabs);
}

/** Open a terminal in the active workspace. `beforeCommit` runs with the new tab before it reaches
 *  the store, which is the layout's chance to say where its panel should go. */
export async function newTab(beforeCommit) {
  const workspaceId = state.activeWorkspaceId;
  if (!workspaceId) return null;
  const tab = await api.createTab(workspaceId);
  beforeCommit?.(tab);
  setState('tabs', (tabs) => [...tabs, tab]);
  return tab;
}

/** Drop a terminal from the page. `destroy` also kills the process behind it. */
export async function closeTab(tabId, destroy) {
  setState('tabs', (tabs) => tabs.filter((t) => t.id !== tabId));
  if (destroy) await api.deleteTab(tabId);
}

/** Put a still-running terminal the browser had let go of back on the page. */
export function adoptTab(tab) {
  batch(() => {
    setState('tabs', (tabs) => [...tabs, tab]);
    setState('focusTabId', tab.id);
  });
}

/** Remove a terminal the server no longer has (its process died, or it was killed elsewhere). */
export function forgetTab(tabId) {
  setState('tabs', (tabs) => tabs.filter((t) => t.id !== tabId));
}

export async function createProject(repoUrl, name, setup, defaultBranch) {
  const project = await api.createProject(repoUrl, name, setup, defaultBranch);
  await refreshProjects();
  return project;
}

export async function updateProject(projectId, patch) {
  await api.updateProject(projectId, patch);
  await refreshProjects();
}

export async function deleteProject(projectId) {
  await api.deleteProject(projectId);
  await refreshProjects();
}

export async function createWorkspace(projectId, name, ref) {
  const workspace = await api.createWorkspace(projectId, name, ref);
  await refreshProjects();
  // Creating it already opened its first terminal; adopt that rather than asking for another.
  ++switchToken;
  batch(() => {
    setState('activeWorkspaceId', workspace.id);
    setState('tabs', [workspace.tab]);
  });
  rememberWorkspace(workspace.id);
  return workspace;
}

export async function deleteWorkspace(workspaceId) {
  await api.deleteWorkspace(workspaceId);
  if (workspaceId === state.activeWorkspaceId) {
    ++switchToken;
    batch(() => {
      setState('activeWorkspaceId', '');
      setState('tabs', []);
    });
    localStorage.removeItem(LAST_WORKSPACE_KEY);
  }
  localStorage.removeItem(`workbench.layout.${workspaceId}`);
  await refreshProjects();
}

export async function init() {
  await refreshProjects();
  const params = new URLSearchParams(location.search);
  const known = new Set(state.projects.flatMap((p) => p.workspaces.map((w) => w.id)));
  const wanted = [params.get('workspace'), localStorage.getItem(LAST_WORKSPACE_KEY)].find(
    (id) => id && known.has(id),
  );
  const wantedTab = params.get('tab');
  if (wanted) {
    await openWorkspace(wanted);
    if (wantedTab && state.tabs.some((t) => t.id === wantedTab)) setState('focusTabId', wantedTab);
  }
  setState('ready', true);
}
