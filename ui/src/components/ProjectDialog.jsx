import { Show, createSignal } from 'solid-js';

import { createProject, updateProject } from '../store';
import { Choice, Field, Modal } from './Modal';

/** Add a project, or edit an existing one. A project's repo URL is fixed once set: its mirror and
 *  workspaces are clones of that repo, so pointing it somewhere else would just be a new project.
 *  A folder project has no repo at all, and its workspaces start as empty directories — until it is
 *  given one, once, here. */
export function ProjectDialog(props) {
  const editing = () => props.project;
  const [kind, setKind] = createSignal(editing() && !editing().repo_url ? 'folder' : 'repo');
  const [repoUrl, setRepoUrl] = createSignal(editing()?.repo_url || '');
  const [name, setName] = createSignal(editing()?.name || '');
  const [setup, setSetup] = createSignal(editing()?.setup || '');
  const [defaultBranch, setDefaultBranch] = createSignal(editing()?.default_branch || '');
  const isRepo = () => kind() === 'repo';
  /** A folder project being configured: it can be given its repo, once, now that one exists. */
  const attaching = () => editing() && !isRepo();

  const submit = () => {
    if (editing()) {
      const patch = { name: name(), setup: setup() };
      if (isRepo()) patch.default_branch = defaultBranch().trim();
      else if (repoUrl().trim()) {
        patch.repo_url = repoUrl().trim();
        patch.default_branch = defaultBranch().trim();
      }
      return updateProject(editing().id, patch);
    }
    return isRepo()
      ? createProject(repoUrl().trim(), name().trim(), setup().trim(), defaultBranch().trim())
      : createProject('', name().trim(), setup().trim(), '');
  };

  return (
    <Modal
      title={editing() ? `Configure ${editing().name}` : 'Add a project'}
      submitLabel={editing() ? 'Save' : 'Add project'}
      onSubmit={submit}
      onClose={props.onClose}
    >
      <Show when={!editing()}>
        <Choice
          label="Kind"
          value={kind}
          onChange={setKind}
          options={[
            { value: 'repo', label: 'Git repo' },
            { value: 'folder', label: 'Folder (no repo)' },
          ]}
          hint={
            isRepo()
              ? 'Each workspace is a fresh clone of the repo.'
              : 'Each workspace starts as an empty directory: for something with no repo yet, or work across several repos cloned into it.'
          }
        />
      </Show>
      <Show when={isRepo()}>
        <Field
          label="Git URL"
          value={repoUrl}
          onInput={setRepoUrl}
          disabled={!!editing()}
          placeholder="https://github.com/owner/repo.git"
          hint={editing() ? 'Fixed once the project exists.' : ''}
        />
      </Show>
      <Show when={attaching()}>
        <Field
          label="Git URL (optional)"
          value={repoUrl}
          onInput={setRepoUrl}
          placeholder="https://github.com/owner/repo.git"
          hint="Turns this into a repo project: new workspaces become clones of it. It needs a commit pushed, and every workspace here must already be a git repo with a commit. Can't be undone."
        />
      </Show>
      <Field
        label={isRepo() ? 'Name (optional)' : 'Name'}
        value={name}
        onInput={setName}
        placeholder={isRepo() ? 'defaults to the repo name' : 'my-idea'}
      />
      <Show when={isRepo() || repoUrl().trim()}>
        <Field
          label="Default branch (optional)"
          value={defaultBranch}
          onInput={setDefaultBranch}
          placeholder="defaults to the repo's own default branch"
          hint="New workspaces start from the tip of this branch, freshly fetched from the remote."
        />
      </Show>
      <Field
        label="Setup command (optional)"
        value={setup}
        onInput={setSetup}
        placeholder={isRepo() ? 'just setup' : 'git clone https://github.com/owner/a.git && git clone …'}
        hint="Run once in each new workspace, before Claude starts."
      />
    </Modal>
  );
}
