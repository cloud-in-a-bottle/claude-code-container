import { Show, createSignal, onCleanup, onMount } from 'solid-js';

import { workspaceAgent, workspaceStatus } from '../store';

/** Long enough that running the mouse down the sidebar doesn't trail cards behind it. */
const HOVER_DELAY_MS = 180;
const CARD_GAP = 10;

/** The one-line version of a status: the card's headline, and the dot's accessible name. */
export function headline(status) {
  if (!status) return 'reading git status…';
  switch (status.state) {
    case 'clean':
      return 'no local changes';
    case 'dirty':
      return [
        status.changed && `${status.changed} changed`,
        status.untracked && `${status.untracked} untracked`,
      ]
        .filter(Boolean)
        .join(', ');
    case 'conflicted':
      return `${status.conflicted} conflicted ${status.conflicted === 1 ? 'file' : 'files'}`;
    case 'cloning':
      return 'still being created';
    case 'unavailable':
      return 'git status unavailable';
    default:
      return status.state;
  }
}

/** What an agent's state is called in front of a person. */
export function agentHeadline(agent) {
  switch (agent?.state) {
    case 'working':
      return 'Claude is working';
    case 'waiting':
      return 'Claude is waiting for you';
    case 'idle':
      return 'Claude is here, idle';
    default:
      return '';
  }
}

/** Rough age of a unix timestamp — the card only ever wants the order of magnitude. */
function ago(since) {
  const seconds = Math.max(0, Date.now() / 1000 - since);
  if (seconds < 60) return 'just now';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.round(minutes / 60);
  return hours < 24 ? `${hours} h` : `${Math.round(hours / 24)} d`;
}

/** How the working tree splits up, when there's more to say than the headline already said. */
function breakdown(status) {
  return [status.staged && `${status.staged} staged`, status.unstaged && `${status.unstaged} unstaged`]
    .filter(Boolean)
    .join(' · ');
}

/** The hovering info card. Positioned beside the dot that opened it, clamped to the viewport. */
function StatusCard(props) {
  let el;

  onMount(() => {
    const anchor = props.anchor.getBoundingClientRect();
    const card = el.getBoundingClientRect();
    const left = Math.min(anchor.right + CARD_GAP, window.innerWidth - card.width - 8);
    const top = Math.min(Math.max(8, anchor.top - 8), window.innerHeight - card.height - 8);
    el.style.left = `${left}px`;
    el.style.top = `${top}px`;
    // The card is fixed to the viewport, so a scrolled sidebar would leave it pointing at the
    // wrong row. Cheaper to dismiss it than to follow the row.
    const close = () => props.onClose();
    document.addEventListener('scroll', close, { capture: true, once: true });
    onCleanup(() => document.removeEventListener('scroll', close, { capture: true }));
  });

  const status = () => props.status;

  return (
    <div class="ws-card" ref={el} role="tooltip">
      <div class="ws-card-head">
        <span class={`status-dot state-${status()?.state || 'unknown'}`} />
        <span class="ws-card-title">{headline(status())}</span>
      </div>

      <Show when={props.agent}>
        <div class="ws-card-row ws-agent-line">
          <span class={`agent-dot state-${props.agent.state}`} />
          <span>{agentHeadline(props.agent)}</span>
          <span class="ws-dim"> · {ago(props.agent.since)}</span>
          <Show when={props.agent.agents > 1}>
            <span class="ws-dim"> · {props.agent.agents} sessions</span>
          </Show>
        </div>
        <Show when={props.agent.state !== 'working' && props.agent.message}>
          <div class="ws-card-row ws-subject ws-dim">“{props.agent.message}”</div>
        </Show>
      </Show>

      <Show when={status()}>
        <Show when={status().branch || status().head}>
          <div class="ws-card-row">
            <span class="ws-branch">{status().branch || `detached at ${status().head}`}</span>
            <Show when={status().upstream}>
              <span class="ws-dim"> → {status().upstream}</span>
            </Show>
          </div>
        </Show>

        <Show when={status().ahead || status().behind}>
          <div class="ws-card-row">
            <Show when={status().ahead}>
              <span class="ws-ahead">↑{status().ahead}</span> ahead{' '}
            </Show>
            <Show when={status().behind}>
              <span class="ws-behind">↓{status().behind}</span> behind
            </Show>
          </div>
        </Show>

        <Show when={breakdown(status())}>
          <div class="ws-card-row ws-dim">{breakdown(status())}</div>
        </Show>

        <Show when={status().insertions || status().deletions}>
          <div class="ws-card-row">
            <Show when={status().insertions}>
              <span class="ws-add">+{status().insertions}</span>{' '}
            </Show>
            <Show when={status().deletions}>
              <span class="ws-del">−{status().deletions}</span>{' '}
            </Show>
            <span class="ws-dim">since the last commit</span>
          </div>
        </Show>

        <Show when={status().subject}>
          <div class="ws-card-row ws-subject">
            <span class="ws-sha">{status().head}</span> {status().subject}
          </div>
          <div class="ws-card-row ws-dim">committed {status().committed}</div>
        </Show>

        <Show when={status().detail}>
          <div class="ws-card-row ws-dim">{status().detail}</div>
        </Show>
      </Show>

      <div class="ws-card-path">{props.path}</div>
    </div>
  );
}

/** The dot at the head of a workspace row: git state at a glance, the rest on hover. */
export function WorkspaceStatusDot(props) {
  const [anchor, setAnchor] = createSignal(null);
  let timer;

  const status = () => workspaceStatus(props.workspace.id);
  const agent = () => workspaceAgent(props.workspace.id);
  const clear = () => clearTimeout(timer);
  onCleanup(clear);

  return (
    <span
      class="ws-status"
      onMouseEnter={(e) => {
        // The row, not the dot: the card goes beside the sidebar rather than over the rows below.
        const el = e.currentTarget.closest('.workspace-row') || e.currentTarget;
        clear();
        timer = setTimeout(() => setAnchor(el), HOVER_DELAY_MS);
      }}
      onMouseLeave={() => {
        clear();
        setAnchor(null);
      }}
    >
      <span
        class={`status-dot state-${status()?.state || 'unknown'}`}
        aria-label={`${props.workspace.name}: ${headline(status())}`}
      />
      <Show when={anchor()}>
        <StatusCard
          anchor={anchor()}
          status={status()}
          agent={agent()}
          path={props.workspace.path}
          onClose={() => setAnchor(null)}
        />
      </Show>
    </span>
  );
}

/** The agent's own dot, beside the git one. Shown only while a session is working or waiting —
 *  an idle Claude sitting at its prompt is not news, and the rail is 220px wide. */
export function WorkspaceAgentDot(props) {
  const agent = () => workspaceAgent(props.workspace.id);
  return (
    <Show when={agent()?.state === 'working' || agent()?.state === 'waiting'}>
      <span
        class={`agent-dot state-${agent().state}`}
        aria-label={`${props.workspace.name}: ${agentHeadline(agent())}`}
      />
    </Show>
  );
}

/** How far a workspace has drifted from its upstream, small enough to live in the row itself. */
export function WorkspaceSync(props) {
  const status = () => workspaceStatus(props.workspace.id);
  return (
    <Show when={status()?.ahead || status()?.behind}>
      <span class="ws-sync">
        <Show when={status().ahead}>
          <span class="ws-ahead">↑{status().ahead}</span>
        </Show>
        <Show when={status().behind}>
          <span class="ws-behind">↓{status().behind}</span>
        </Show>
      </span>
    </Show>
  );
}
