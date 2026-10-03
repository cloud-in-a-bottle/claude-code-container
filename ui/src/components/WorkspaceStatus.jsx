import { Show, createSignal, onCleanup, onMount } from 'solid-js';

import { workspaceAgent, workspaceStatus } from '../store';

/** Long enough that running the mouse down the sidebar doesn't trail cards behind it. */
const HOVER_DELAY_MS = 180;
const CARD_GAP = 10;

/** The headline of the hover card: what the dot means, in words.
 *
 *  The dot answers "how far is this work from done", so the states run in that order — nothing
 *  here, uncommitted, unpushed, no PR yet, then whatever the PR itself is doing. */
export function headline(view) {
  if (!view) return 'reading git status…';
  const git = view.git || {};
  const pr = view.pull_request;
  switch (view.dot) {
    case 'untouched':
      return 'nothing of its own yet';
    case 'uncommitted':
      return [git.changed && `${git.changed} changed`, git.untracked && `${git.untracked} untracked`]
        .filter(Boolean)
        .join(', ');
    case 'unpushed':
      return `${git.ahead} to push`;
    case 'unreviewed':
      return 'pushed, no PR yet';
    case 'pr_open':
      return `PR #${pr?.number} ${pr?.state === 'draft' ? 'draft' : 'open'}`;
    case 'pr_merged':
      return `PR #${pr?.number} merged`;
    case 'pr_closed':
      return `PR #${pr?.number} closed`;
    case 'conflicted':
      return `${git.conflicted} conflicted ${git.conflicted === 1 ? 'file' : 'files'}`;
    case 'cloning':
      return 'still being created';
    case 'unavailable':
      return 'git status unavailable';
    default:
      return view.dot;
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
function breakdown(git) {
  return [git.staged && `${git.staged} staged`, git.unstaged && `${git.unstaged} unstaged`]
    .filter(Boolean)
    .join(' · ');
}

/** The hovering info card. Positioned beside the row that opened it, clamped to the viewport. */
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

  const view = () => props.view;
  const git = () => props.view?.git || {};

  return (
    <div class="ws-card" ref={el} role="tooltip">
      <div class="ws-card-head">
        <span class={`status-dot state-${view()?.dot || 'unknown'}`} />
        <span class="ws-card-title">{headline(view())}</span>
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

      <Show when={view()?.pull_request}>
        <div class="ws-card-row ws-subject">
          <span class="ws-dim">#{view().pull_request.number}</span> {view().pull_request.title}
        </div>
      </Show>

      <Show when={view()}>
        <Show when={git().branch || git().head}>
          <div class="ws-card-row">
            <span class="ws-branch">{git().branch || `detached at ${git().head}`}</span>
            <Show when={git().upstream}>
              <span class="ws-dim"> → {git().upstream}</span>
            </Show>
          </div>
        </Show>

        <Show when={git().ahead || git().behind}>
          <div class="ws-card-row">
            <Show when={git().ahead}>
              <span class="ws-ahead">↑{git().ahead}</span> ahead{' '}
            </Show>
            <Show when={git().behind}>
              <span class="ws-behind">↓{git().behind}</span> behind
            </Show>
          </div>
        </Show>

        <Show when={breakdown(git())}>
          <div class="ws-card-row ws-dim">{breakdown(git())}</div>
        </Show>

        <Show when={git().insertions || git().deletions}>
          <div class="ws-card-row">
            <Show when={git().insertions}>
              <span class="ws-add">+{git().insertions}</span>{' '}
            </Show>
            <Show when={git().deletions}>
              <span class="ws-del">−{git().deletions}</span>{' '}
            </Show>
            <span class="ws-dim">since the last commit</span>
          </div>
        </Show>

        <Show when={git().subject}>
          <div class="ws-card-row ws-subject">
            <span class="ws-sha">{git().head}</span> {git().subject}
          </div>
          <div class="ws-card-row ws-dim">committed {git().committed}</div>
        </Show>

        <Show when={git().detail}>
          <div class="ws-card-row ws-dim">{git().detail}</div>
        </Show>
      </Show>

      <div class="ws-card-path">{props.path}</div>
    </div>
  );
}

/** The dot at the head of a workspace row: how far this work is from done, the rest on hover. */
export function WorkspaceStatusDot(props) {
  const [anchor, setAnchor] = createSignal(null);
  let timer;

  const view = () => workspaceStatus(props.workspace.id);
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
        class={`status-dot state-${view()?.dot || 'unknown'}`}
        aria-label={`${props.workspace.name}: ${headline(view())}`}
      />
      <Show when={anchor()}>
        <StatusCard
          anchor={anchor()}
          view={view()}
          agent={agent()}
          path={props.workspace.path}
          onClose={() => setAnchor(null)}
        />
      </Show>
    </span>
  );
}

/** The agent's own glyph, beside the git one. Shown only while a session is working or waiting —
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
  const git = () => workspaceStatus(props.workspace.id)?.git;
  return (
    <Show when={git()?.ahead || git()?.behind}>
      <span class="ws-sync">
        <Show when={git().ahead}>
          <span class="ws-ahead">↑{git().ahead}</span>
        </Show>
        <Show when={git().behind}>
          <span class="ws-behind">↓{git().behind}</span>
        </Show>
      </span>
    </Show>
  );
}
