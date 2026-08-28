import { Show, createEffect, onCleanup } from 'solid-js';

import {
  DEFAULT_SIDE,
  HOME_URL,
  hidden,
  setHidden,
  setUrl,
  setWidth,
  url,
  width,
} from '../sidePanel';
import { theme } from '../store';

/** A resizable pane beside the terminal that loads any URL in an iframe.
 *
 *  The terminal is refitted by dispatching a window resize event rather than reaching into the
 *  panes: every TerminalPane already listens for exactly that. */
export function SidePanel() {
  let frame;
  let input;

  const relayout = () => window.dispatchEvent(new Event('resize'));

  function startDrag(e) {
    e.preventDefault();
    document.body.classList.add('sp-dragging');
    const onMove = (ev) => {
      setWidth(window.innerWidth - ev.clientX);
      relayout();
    };
    const onUp = () => {
      document.body.classList.remove('sp-dragging');
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      relayout();
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  }

  function onDividerKey(e) {
    const step = e.shiftKey ? 60 : 15;
    if (e.key === 'ArrowLeft') setWidth(width() + step);
    else if (e.key === 'ArrowRight') setWidth(width() - step);
    else return;
    e.preventDefault();
    relayout();
  }

  function navigate(e) {
    e.preventDefault();
    const value = input.value.trim();
    if (value) setUrl(value);
  }

  // Hiding or resizing changes how much room the terminal has.
  createEffect(() => {
    hidden();
    width();
    queueMicrotask(relayout);
  });

  // The panel is a separate same-origin document, so it can be restyled in place.
  createEffect(() => {
    const name = theme();
    if (!frame) return;
    const paint = () => {
      try {
        frame.contentDocument?.documentElement?.setAttribute('data-theme', name);
      } catch (_) {
        /* cross-origin: not ours to touch */
      }
    };
    paint();
    frame.addEventListener('load', paint);
    onCleanup(() => frame.removeEventListener('load', paint));
  });

  return (
    <Show when={!hidden()}>
      <div
        id="sp-divider"
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize side panel"
        tabIndex={0}
        onMouseDown={startDrag}
        onDblClick={() => setWidth(DEFAULT_SIDE)}
        onKeyDown={onDividerKey}
      />
      <aside id="sp-side" style={{ flex: `0 0 ${width()}px` }}>
        <form id="sp-bar" onSubmit={navigate}>
          <input id="sp-url" ref={input} value={url()} spellcheck={false} aria-label="Side panel URL" />
          <button class="sp-btn" type="button" title="Reload" onClick={() => frame.contentWindow.location.reload()}>
            &#8635;
          </button>
          <button class="sp-btn" type="button" title="Open in new tab" onClick={() => window.open(url(), '_blank')}>
            &#8599;
          </button>
          <button class="sp-btn" type="button" title="Hide panel" onClick={() => setHidden(true)}>
            &times;
          </button>
        </form>
        <iframe id="sp-frame" ref={frame} src={url() || HOME_URL} title="Side panel" />
      </aside>
    </Show>
  );
}
