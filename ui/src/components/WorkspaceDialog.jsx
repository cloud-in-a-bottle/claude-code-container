import { Show, createSignal } from 'solid-js';

import { BILLING_LABELS, billingWarning } from '../billing';
import { createWorkspace, state } from '../store';
import { Choice, Field, Modal } from './Modal';

/** Create a workspace: another full copy of the project's repo, cloned from its local mirror. */
export function WorkspaceDialog(props) {
  const [name, setName] = createSignal('');
  const [ref, setRef] = createSignal('');
  // The workbench default until the user says otherwise. Fixed for the life of the workspace once
  // it's created, so it is offered here rather than only in Settings.
  const [billing, setBilling] = createSignal(state.settings?.default_billing || 'api');
  const fallback = () => props.project.default_branch || "the repo's default branch";
  const warning = () => billingWarning(billing(), state.settings);

  return (
    <Modal
      title={`New workspace in ${props.project.name}`}
      submitLabel="Create"
      onSubmit={() => createWorkspace(props.project.id, name().trim(), ref().trim(), billing())}
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
      <Choice
        label="Billing"
        value={billing}
        onChange={setBilling}
        options={[
          { value: 'api', label: BILLING_LABELS.api },
          { value: 'subscription', label: BILLING_LABELS.subscription },
        ]}
        hint="How this workspace's Claude sessions are paid for. Fixed once the workspace exists."
      />
      <Show when={warning()}>
        <p class="wb-warn">{warning()}</p>
      </Show>
    </Modal>
  );
}
