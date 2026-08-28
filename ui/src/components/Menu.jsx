import { For, Show, onCleanup, onMount } from 'solid-js';

/** A popup anchored under the element that opened it. Items are
 *  `{ label, description?, action }`, or null for a separator. */
export function Menu(props) {
  let el;

  onMount(() => {
    const rect = props.anchor.getBoundingClientRect();
    el.style.left = `${rect.left}px`;
    el.style.top = `${rect.bottom}px`;
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
