import { batch, createSignal } from 'solid-js';
import { createStore } from 'solid-js/store';

import * as api from './api';
import { DEFAULT_THEME, THEMES } from './themes';

const bootstrap = window.__WORKBENCH__ || {};

const LAST_WORKSPACE_KEY = 'workbench.workspace';
const SIDEBAR_HIDDEN_KEY = 'workbench.sidebarHidden';

/** How often the sidebar re-reads git status. Unhurried on purpose: the server coalesces bursts,
 *  but this is still `git` on a one-core container shared with the Claude sessions. */
const STATUS_POLL_MS = 10000;

/** Agent state costs a few small file reads rather than a `git` process, and "is it working right
 *  now" is only worth showing if it keeps up, so it gets its own faster poll. */
const AGENT_POLL_MS = 3000;

const [state, setState] = createStore({
  projects: [],
  /** Terminals of the active workspace only — switching workspaces replaces this wholesale.
   *  Which of them is visible, and how they're arranged, is dockview's business, not the store's. */
  tabs: [],
  activeWorkspaceId: '',
  /** A terminal the layout should bring to the front once its panel exists. Cleared when used. */
  focusTabId: '',
  /** Git status of every workspace, keyed by workspace id — what the sidebar's dots read. Filled
   *  by a poll, so a workspace is missing from it until the first one lands. */
  status: {},
  /** What Claude is doing, keyed by workspace id. Only workspaces with a live session appear. */
  agents: {},
  /** Workbench settings — the billing default and what each mode has credentials for. Null until
   *  the first load lands, which is the settings page's "checking…" state. */
  settings: null,
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

/** Re-read the settings, including a fresh `claude auth status` probe in the container. */
export async function refreshSettings() {
  setState('settings', await api.getSettings());
}

/** Change the billing mode new workspaces get. Existing workspaces keep what they were made with,
 *  so this never moves work in progress onto another account. */
export async function saveDefaultBilling(mode) {
  setState('settings', await api.saveSettings({ default_billing: mode }));
}

export async function refreshStatus() {
  const statuses = await api.listWorkspaceStatus();
  setState('status', Object.fromEntries(statuses.map((s) => [s.workspace_id, s])));
}

export function workspaceStatus(workspaceId) {
  return state.status[workspaceId];
}

export async function refreshAgents() {
  const agents = await api.listWorkspaceAgents();
  setState('agents', Object.fromEntries(agents.map((a) => [a.workspace_id, a])));
}

export function workspaceAgent(workspaceId) {
  return state.agents[workspaceId];
}

/** Ask for fresh statuses without waiting on them: a failed refresh only means the dots stay as
 *  they are until the next poll, which is not worth interrupting anyone over. Worth saying once,
 *  though -- a rail full of grey dots is otherwise a mystery with nothing in the console. */
let statusFailureReported = false;
function refreshStatusSoon() {
  refreshStatus().catch((err) => {
    if (statusFailureReported) return;
    statusFailureReported = true;
    console.warn('could not read workspace status; the sidebar dots may be stale', err);
  });
}

/** Keep the status dots current, and only while the page is actually being looked at. A failed
 *  poll leaves the last statuses on screen rather than blanking them; the next tick tries again. */
function watchStatus() {
  const tick = () => {
    if (document.visibilityState !== 'visible') return;
    refreshStatusSoon();
    refreshAgents().catch(() => {});
  };
  const agentTick = () => {
    if (document.visibilityState === 'visible') refreshAgents().catch(() => {});
  };
  setInterval(tick, STATUS_POLL_MS);
  setInterval(agentTick, AGENT_POLL_MS);
  // A tab left in the background misses ticks, so catch up the moment it comes back.
  document.addEventListener('visibilitychange', tick);
  tick();
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

export async function createWorkspace(projectId, name, ref, billing) {
  const workspace = await api.createWorkspace(projectId, name, ref, billing);
  await refreshProjects();
  refreshStatusSoon();
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
  refreshStatusSoon();
}

export async function init() {
  await refreshProjects();
  // Before the restore below, not after it. Nothing about the sidebar's dots depends on which
  // workspace is being reopened, while the restore spawns terminals and is the likeliest step
  // here to fail or to take its time -- and when it did, the poll never started at all and every
  // dot in the rail stayed grey until someone reloaded the page.
  watchStatus();
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
  // Not awaited: it shells out to `claude auth status`, and nothing on screen at this point is
  // waiting on the answer. The dialogs that need it re-read it when they open.
  refreshSettings().catch(() => console.warn('could not load the workbench settings'));
}
