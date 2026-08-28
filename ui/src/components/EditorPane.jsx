import { Match, Switch, createSignal, onMount } from 'solid-js';

import * as api from '../api';

/** A VS Code instance for one workspace, in an iframe on the workbench's own origin.
 *
 *  The instance lives server-side and is started on demand, so the panel's job is to ask for one,
 *  say what's happening while it comes up (the very first ever also downloads code-server), and
 *  then get out of the way. Reloading the page or reopening the panel reattaches to the same
 *  instance with its editors and terminals intact. */
export function EditorPane(props) {
  const [state, setState] = createSignal({ status: 'starting' });

  async function start() {
    setState({ status: 'starting' });
    try {
      const editor = await api.startEditor(props.workspaceId);
      setState({ status: 'ready', url: editor.url });
    } catch (err) {
      setState({ status: 'failed', message: err.message });
    }
  }

  onMount(start);

  return (
    <div class="editor-panel">
      <Switch>
        <Match when={state().status === 'starting'}>
          <div class="editor-status">
            <div>Starting the editor…</div>
            <div class="editor-status-hint">The first one also downloads it, which takes a moment.</div>
          </div>
        </Match>
        <Match when={state().status === 'failed'}>
          <div class="editor-status">
            <div>Could not start the editor.</div>
            <div class="editor-status-hint">{state().message}</div>
            <button class="wb-btn" type="button" onClick={start}>
              Try again
            </button>
          </div>
        </Match>
        <Match when={state().status === 'ready'}>
          {/* Same origin as the workbench, so it inherits the router's authentication and needs no
              sandbox exceptions to reach its own websocket. */}
          <iframe src={state().url} title={`editor for ${props.workspaceId}`} allow="clipboard-read; clipboard-write" />
        </Match>
      </Switch>
    </div>
  );
}
