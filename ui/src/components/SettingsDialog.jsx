import { Show, createSignal, onCleanup, onMount } from 'solid-js';

import { BILLING_LABELS } from '../billing';
import { refreshSettings, saveDefaultBilling, state } from '../store';

/** The settings page: which billing mode new workspaces get, and whether each mode has anything
 *  to bill to. The subscription half is where `claude auth login` gets explained, since that is
 *  the one credential the workbench can't fetch for you.
 *
 *  Reads through to the server on open — the login state lives in ~/.claude and can change from
 *  any terminal, so a cached answer here would be wrong exactly when it matters.
 */
export function SettingsDialog(props) {
  const [error, setError] = createSignal('');
  const [checking, setChecking] = createSignal(false);
  const settings = () => state.settings;
  const subscription = () => settings()?.subscription;

  async function recheck() {
    setError('');
    setChecking(true);
    try {
      await refreshSettings();
    } catch (err) {
      setError(err.message || String(err));
    } finally {
      setChecking(false);
    }
  }

  async function chooseDefault(mode) {
    setError('');
    try {
      await saveDefaultBilling(mode);
    } catch (err) {
      setError(err.message || String(err));
    }
  }

  onMount(() => {
    recheck();
    const onKey = (e) => e.key === 'Escape' && props.onClose();
    window.addEventListener('keydown', onKey);
    onCleanup(() => window.removeEventListener('keydown', onKey));
  });

  return (
    <div id="wb-modal-backdrop" onClick={(e) => e.target.id === 'wb-modal-backdrop' && props.onClose()}>
      <div class="wb-modal wb-settings">
        <h2>Settings</h2>

        <section>
          <h3>Billing for new workspaces</h3>
          <label>
            Default
            <select
              value={settings()?.default_billing || 'api'}
              disabled={!settings()}
              onChange={(e) => chooseDefault(e.currentTarget.value)}
            >
              <option value="api">{BILLING_LABELS.api}</option>
              <option value="subscription">{BILLING_LABELS.subscription}</option>
            </select>
          </label>
          <p class="wb-hint">
            Each workspace is created with one of these and keeps it, so changing this leaves
            existing workspaces — and the conversations in them — on what they started with. The
            workspace dialog can override it per workspace.
          </p>
        </section>

        <section>
          <h3>Claude subscription</h3>
          <Show when={subscription()} fallback={<p class="wb-status">checking…</p>}>
            <Show
              when={subscription().logged_in}
              fallback={
                <>
                  <p class="wb-status bad">Not signed in.</p>
                  <p class="wb-hint">
                    Run <code>claude auth login</code> in any terminal and follow the link it
                    prints. Credentials are stored in <code>~/.claude</code>, which is on the
                    app's persistent disk, so this survives redeploys and only has to be done
                    once.
                  </p>
                  <Show when={subscription().detail}>
                    <p class="wb-hint">{subscription().detail}</p>
                  </Show>
                </>
              }
            >
              <p class="wb-status good">
                Signed in{subscription().account ? ` as ${subscription().account}` : ''}.
              </p>
              <p class="wb-hint">
                Subscription-billed workspaces use these credentials. <code>claude auth logout</code>{' '}
                in a terminal signs out.
              </p>
            </Show>
          </Show>
        </section>

        <section>
          <h3>API key</h3>
          <Show when={settings()} fallback={<p class="wb-status">checking…</p>}>
            <Show
              when={settings().api_key_available}
              fallback={
                <>
                  <p class="wb-status bad">No ANTHROPIC_API_KEY.</p>
                  <p class="wb-hint">
                    Add <code>ANTHROPIC_API_KEY</code> to the secrets app and restart this app, or
                    export it by hand in a terminal.
                  </p>
                </>
              }
            >
              <p class="wb-status good">Available from the secrets app.</p>
            </Show>
          </Show>
        </section>

        <Show when={error()}>
          <p class="wb-error">{error()}</p>
        </Show>
        <div class="wb-actions">
          <button class="wb-btn" type="button" disabled={checking()} onClick={recheck}>
            {checking() ? 'Checking…' : 'Check again'}
          </button>
          <button class="wb-btn" type="button" onClick={props.onClose}>
            Done
          </button>
        </div>
      </div>
    </div>
  );
}
