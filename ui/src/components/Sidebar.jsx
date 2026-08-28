import { For, Show, createSignal } from 'solid-js';

import { deleteProject, deleteWorkspace, openWorkspace, state, toggleSidebar } from '../store';
import { ConfirmDialog } from './ConfirmDialog';
import { ProjectDialog } from './ProjectDialog';
import { WorkspaceDialog } from './WorkspaceDialog';
import { WorkspaceStatusDot, WorkspaceSync } from './WorkspaceStatus';

const COLLAPSED_KEY = 'workbench.collapsedProjects';

function loadCollapsed() {
  try {
    return new Set(JSON.parse(localStorage.getItem(COLLAPSED_KEY) || '[]'));
  } catch (_) {
    return new Set();
  }
}

/** The projects → workspaces rail. A project is a git repo; a workspace is one copy of it. */
export function Sidebar() {
  const [dialog, setDialog] = createSignal(null);
  const [collapsed, setCollapsed] = createSignal(loadCollapsed());

  function toggleProject(projectId) {
    const next = new Set(collapsed());
    if (next.has(projectId)) next.delete(projectId);
    else next.add(projectId);
    setCollapsed(next);
    localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...next]));
  }

  const stop = (fn) => (e) => {
    e.stopPropagation();
    fn();
  };

  return (
    <>
      <aside id="sidebar">
        <div id="sidebar-head">
          <span id="sidebar-title">Projects</span>
          <button id="add-project" type="button" title="Add a project" onClick={() => setDialog({ kind: 'project' })}>
            +
          </button>
          <button id="sidebar-collapse" type="button" title="Hide the sidebar" onClick={toggleSidebar}>
            &#8676;
          </button>
        </div>
        <div id="project-list">
          <Show when={state.projects.length} fallback={<p class="sidebar-empty">No projects yet. Add one with +.</p>}>
            <For each={state.projects}>
              {(project) => (
                <div class="project">
                  <div class="project-row" onClick={() => toggleProject(project.id)} title={project.repo_url}>
                    <span class="project-caret">{collapsed().has(project.id) ? '▶' : '▼'}</span>
                    <span class="project-name">{project.name}</span>
                    <button
                      class="row-btn"
                      type="button"
                      title="New workspace"
                      onClick={stop(() => setDialog({ kind: 'workspace', project }))}
                    >
                      +
                    </button>
                    <button
                      class="row-btn"
                      type="button"
                      title="Configure project"
                      onClick={stop(() => setDialog({ kind: 'project', project }))}
                    >
                      &#9881;
                    </button>
                    <button
                      class="row-btn danger"
                      type="button"
                      title="Remove project"
                      onClick={stop(() => setDialog({ kind: 'delete-project', project }))}
                    >
                      &times;
                    </button>
                  </div>
                  <Show when={!collapsed().has(project.id)}>
                    <Show
                      when={project.workspaces.length}
                      fallback={<div class="project-empty">no workspaces</div>}
                    >
                      <For each={project.workspaces}>
                        {(workspace) => (
                          <div
                            class="workspace-row"
                            classList={{ active: workspace.id === state.activeWorkspaceId }}
                            onClick={() => openWorkspace(workspace.id)}
                          >
                            {/* The dot carries the path and the rest of the git status in its
                                hover card, which is why the row has no title of its own. */}
                            <WorkspaceStatusDot workspace={workspace} />
                            <span class="workspace-name">{workspace.name}</span>
                            <WorkspaceSync workspace={workspace} />
                            <button
                              class="row-btn danger"
                              type="button"
                              title="Delete workspace"
                              onClick={stop(() => setDialog({ kind: 'delete-workspace', project, workspace }))}
                            >
                              &times;
                            </button>
                          </div>
                        )}
                      </For>
                    </Show>
                  </Show>
                </div>
              )}
            </For>
          </Show>
        </div>
      </aside>

      <Show when={dialog()?.kind === 'project'}>
        <ProjectDialog project={dialog().project} onClose={() => setDialog(null)} />
      </Show>
      <Show when={dialog()?.kind === 'workspace'}>
        <WorkspaceDialog project={dialog().project} onClose={() => setDialog(null)} />
      </Show>
      <Show when={dialog()?.kind === 'delete-workspace'}>
        <ConfirmDialog
          title={`Delete workspace "${dialog().workspace.name}"?`}
          message={`This deletes ${dialog().workspace.path} and everything in it, and closes its terminals. It cannot be undone.`}
          submitLabel="Delete"
          onConfirm={() => deleteWorkspace(dialog().workspace.id)}
          onClose={() => setDialog(null)}
        />
      </Show>
      <Show when={dialog()?.kind === 'delete-project'}>
        <ConfirmDialog
          title={`Remove project "${dialog().project.name}"?`}
          message="This forgets the project and deletes its local mirror. Delete its workspaces first if it still has any."
          submitLabel="Remove"
          onConfirm={() => deleteProject(dialog().project.id)}
          onClose={() => setDialog(null)}
        />
      </Show>
    </>
  );
}
