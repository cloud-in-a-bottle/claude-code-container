/** Thin wrapper over the workbench's JSON API. Every call throws an Error carrying the server's
 *  own message, so callers can put it straight in front of the user. */
async function result(resp, method, url) {
  const text = await resp.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch (_) {
    data = null; // an error page from somewhere in front of us, not the app
  }
  if (!resp.ok) {
    throw new Error((data && (data.message || data.error)) || `${method} ${url} failed (${resp.status})`);
  }
  return data;
}

async function request(method, url, body) {
  const resp = await fetch(url, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return result(resp, method, url);
}

export const listProjects = () => request('GET', '/api/projects');
export const createProject = (repoUrl, name, setup, defaultBranch) =>
  request('POST', '/api/projects', { repo_url: repoUrl, name, setup, default_branch: defaultBranch });
export const updateProject = (id, patch) => request('PATCH', `/api/projects/${encodeURIComponent(id)}`, patch);
export const deleteProject = (id) => request('DELETE', `/api/projects/${encodeURIComponent(id)}`);

export const createWorkspace = (projectId, name, ref) =>
  request('POST', '/api/workspaces', { project_id: projectId, name, ref });
export const deleteWorkspace = (workspaceId) =>
  request('DELETE', `/api/workspaces/${workspaceId.split('/').map(encodeURIComponent).join('/')}`);

/** Git status for every workspace in one call — see the sidebar's status dots. */
export const listWorkspaceStatus = () => request('GET', '/api/workspaces/status');

export const listTabs = (workspaceId) =>
  request('GET', `/api/tabs?workspace=${encodeURIComponent(workspaceId)}`);
export const createTab = (workspaceId, label) => request('POST', '/api/tabs', { workspace_id: workspaceId, label });
export const deleteTab = (tabId) => request('DELETE', `/api/tabs/${encodeURIComponent(tabId)}`);
export const kickTab = (tabId) => request('POST', `/api/tabs/${encodeURIComponent(tabId)}/kick`);

/** Store an image from the browser's clipboard; resolves to the path it was given in the
 *  container. Sent as raw bytes rather than JSON so a screenshot isn't base64'd on the way. */
export async function uploadPastedImage(blob) {
  const url = '/api/pasted-images';
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': blob.type || 'application/octet-stream' },
    body: blob,
  });
  return result(resp, 'POST', url);
}

export const saveUiSettings = (patch) => request('POST', '/api/ui/settings', patch);

export const listEditors = () => request('GET', '/api/editor');
export const startEditor = (workspaceId) => request('POST', '/api/editor', { workspace_id: workspaceId });
export const stopEditor = (workspaceId) =>
  request('DELETE', `/api/editor/${workspaceId.split('/').map(encodeURIComponent).join('/')}`);
