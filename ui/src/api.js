/** Thin wrapper over the workbench's JSON API. Every call throws an Error carrying the server's
 *  own message, so callers can put it straight in front of the user. */
async function request(method, url, body) {
  const resp = await fetch(url, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await resp.text();
  const data = text ? JSON.parse(text) : null;
  if (!resp.ok) {
    throw new Error((data && (data.message || data.error)) || `${method} ${url} failed (${resp.status})`);
  }
  return data;
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

export const listTabs = (workspaceId) =>
  request('GET', `/api/tabs?workspace=${encodeURIComponent(workspaceId)}`);
export const createTab = (workspaceId, label) => request('POST', '/api/tabs', { workspace_id: workspaceId, label });
export const deleteTab = (tabId) => request('DELETE', `/api/tabs/${encodeURIComponent(tabId)}`);
export const kickTab = (tabId) => request('POST', `/api/tabs/${encodeURIComponent(tabId)}/kick`);

export const saveUiSettings = (patch) => request('POST', '/api/ui/settings', patch);
