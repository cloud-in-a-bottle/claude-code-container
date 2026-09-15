import { For, Show, onCleanup, onMount } from 'solid-js';

/** How far the menu stays clear of the viewport edge when it has to be pulled back inside. */
const EDGE_GAP = 6;

/** A popup, either under the element that opened it (`anchor`) or at a point (`at`, for a right
 *  click). Items are `{ label, description?, action }`, or null for a separator. */
export function Menu(props) {
  let el;

  onMount(() => {
    const rect = props.anchor?.getBoundingClientRect();
    const wanted = rect ? { x: rect.left, y: rect.bottom } : props.at;
    // Measured after mount rather than guessed: a context menu opened near the bottom of a full
    // sidebar would otherwise hang off the screen with no way to reach its last item.
    const size = el.getBoundingClientRect();
    const left = Math.max(EDGE_GAP, Math.min(wanted.x, window.innerWidth - size.width - EDGE_GAP));
    const top = Math.max(EDGE_GAP, Math.min(wanted.y, window.innerHeight - size.height - EDGE_GAP));
    el.style.left = `${left}px`;
    el.style.top = `${top}px`;
    // Deferred, or the click that opened the menu closes it again.
    const close = () => props.onClose();
    requestAnimationFrame(() => document.addEventListener('click', close, { once: true, capture: true }));
    onCleanup(() => document.removeEventListener('click', close, { capture: true }));
  });

  return (
    <div class="tab-menu" ref={el}>
      <For each={props.items}>
        {(item) => (
          <Show when={item} fallback={<div class="tab-menu-sep" />}>
            <div
              class="tab-menu-item"
              classList={{ danger: item.danger }}
              onClick={() => {
                props.onClose();
                item.action();
              }}
            >
              <div>{item.label}</div>
              <Show when={item.description}>
                <div class="tab-menu-desc">{item.description}</div>
              </Show>
            </div>
          </Show>
        )}
      </For>
    </div>
  );
}
