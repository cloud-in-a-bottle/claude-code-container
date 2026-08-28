import { createSignal } from 'solid-js';

import { createProject, updateProject } from '../store';
import { Field, Modal } from './Modal';

/** Add a project, or edit an existing one. A project's repo URL is fixed once set: its mirror and
 *  workspaces are clones of that repo, so pointing it somewhere else would just be a new project. */
export function ProjectDialog(props) {
  const editing = () => props.project;
  const [repoUrl, setRepoUrl] = createSignal(editing()?.repo_url || '');
  const [name, setName] = createSignal(editing()?.name || '');
  const [setup, setSetup] = createSignal(editing()?.setup || '');
  const [defaultBranch, setDefaultBranch] = createSignal(editing()?.default_branch || '');

  const submit = () =>
    editing()
      ? updateProject(editing().id, {
          name: name(),
          setup: setup(),
          default_branch: defaultBranch().trim(),
        })
      : createProject(repoUrl().trim(), name().trim(), setup().trim(), defaultBranch().trim());

  return (
    <Modal
      title={editing() ? `Configure ${editing().name}` : 'Add a project'}
      submitLabel={editing() ? 'Save' : 'Add project'}
      onSubmit={submit}
      onClose={props.onClose}
    >
      <Field
        label="Git URL"
        value={repoUrl}
        onInput={setRepoUrl}
        disabled={!!editing()}
        placeholder="https://github.com/owner/repo.git"
        hint={editing() ? 'Fixed once the project exists.' : ''}
      />
      <Field label="Name (optional)" value={name} onInput={setName} placeholder="defaults to the repo name" />
      <Field
        label="Default branch (optional)"
        value={defaultBranch}
        onInput={setDefaultBranch}
        placeholder="defaults to the repo's own default branch"
        hint="New workspaces start from the tip of this branch, freshly fetched from the remote."
      />
      <Field
        label="Setup command (optional)"
        value={setup}
        onInput={setSetup}
        placeholder="just setup"
        hint="Run once in each new workspace, before Claude starts."
      />
    </Modal>
  );
}
