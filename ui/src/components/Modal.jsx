import { Show, createSignal, onCleanup, onMount } from 'solid-js';

/** A small centred dialog. Submitting runs `props.onSubmit`, keeps the dialog up while it's in
 *  flight, and shows whatever error it throws instead of closing. */
export function Modal(props) {
  const [error, setError] = createSignal('');
  const [busy, setBusy] = createSignal(false);
  let firstField;

  async function submit(e) {
    e.preventDefault();
    setError('');
    setBusy(true);
    try {
      await props.onSubmit();
      props.onClose();
    } catch (err) {
      setError(err.message || String(err));
    } finally {
      setBusy(false);
    }
  }

  onMount(() => {
    firstField?.querySelector('input')?.focus();
    const onKey = (e) => e.key === 'Escape' && props.onClose();
    window.addEventListener('keydown', onKey);
    onCleanup(() => window.removeEventListener('keydown', onKey));
  });

  return (
    <div id="wb-modal-backdrop" onClick={(e) => e.target.id === 'wb-modal-backdrop' && props.onClose()}>
      <form class="wb-modal" onSubmit={submit}>
        <h2>{props.title}</h2>
        <div ref={firstField}>{props.children}</div>
        <Show when={error()}>
          <p class="wb-error">{error()}</p>
        </Show>
        <div class="wb-actions">
          <button class="wb-btn" type="button" onClick={props.onClose}>
            Cancel
          </button>
          <button class="wb-btn" classList={{ danger: props.danger }} type="submit" disabled={busy()}>
            {busy() ? 'Working…' : props.submitLabel}
          </button>
        </div>
      </form>
    </div>
  );
}

/** A labelled text input for use inside a Modal. */
export function Field(props) {
  return (
    <>
      <label>
        {props.label}
        <input
          value={props.value()}
          placeholder={props.placeholder || ''}
          spellcheck={false}
          disabled={props.disabled}
          onInput={(e) => props.onInput?.(e.currentTarget.value)}
        />
      </label>
      <Show when={props.hint}>
        <p class="wb-hint">{props.hint}</p>
      </Show>
    </>
  );
}
