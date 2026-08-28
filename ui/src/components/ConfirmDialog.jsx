import { Modal } from './Modal';

export function ConfirmDialog(props) {
  return (
    <Modal
      title={props.title}
      submitLabel={props.submitLabel}
      danger
      onSubmit={props.onConfirm}
      onClose={props.onClose}
    >
      <p class="wb-hint">{props.message}</p>
    </Modal>
  );
}
