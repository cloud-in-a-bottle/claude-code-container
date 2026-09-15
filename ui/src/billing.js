/** Shared vocabulary for the two ways a workspace's Claude sessions can be paid for. The strings
 *  themselves are the server's (see src/server/billing.py); these are just how they read. */

export const BILLING_LABELS = {
  api: 'API key',
  subscription: 'Claude subscription',
};

/** The problem with picking `mode` right now, or '' when there isn't one.
 *
 * Both modes can be selected before their credentials exist — a workspace whose Claude comes up
 * asking to be logged in is recoverable, and the settings page says how — so this warns rather
 * than blocking. `settings` may be null, before the first load lands, in which case nothing is
 * known and nothing is claimed.
 */
export function billingWarning(mode, settings) {
  if (!settings) return '';
  if (mode === 'subscription' && !settings.subscription?.logged_in) {
    return 'No subscription is signed in yet — run claude auth login in a terminal, or see Settings.';
  }
  if (mode === 'api' && !settings.api_key_available) {
    return 'No ANTHROPIC_API_KEY found. Add one to the secrets app, or export it in the terminal.';
  }
  return '';
}
