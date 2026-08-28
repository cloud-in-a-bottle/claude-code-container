import { createSignal } from 'solid-js';

import { createWorkspace } from '../store';
import { Field, Modal } from './Modal';

/** Create a workspace: another full copy of the project's repo, cloned from its local mirror. */
export function WorkspaceDialog(props) {
  const [name, setName] = createSignal('');
  const [ref, setRef] = createSignal('');
  const fallback = () => props.project.default_branch || "the repo's default branch";

  return (
    <Modal
      title={`New workspace in ${props.project.name}`}
      submitLabel="Create"
      onSubmit={() => createWorkspace(props.project.id, name().trim(), ref().trim())}
      onClose={props.onClose}
    >
      <Field label="Name" value={name} onInput={setName} placeholder="fix-503" />
      <Field
        label="Ref (optional)"
        value={ref}
        onInput={setRef}
        placeholder={`leave blank for ${fallback()}`}
        hint={`A branch, tag, or commit to check out. Blank starts from ${fallback()}, at its newest commit.`}
      />
    </Modal>
  );
}
