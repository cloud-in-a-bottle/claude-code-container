import { For } from 'solid-js';

import { applyTheme, theme } from '../store';
import { THEMES } from '../themes';

export function ThemePicker() {
  return (
    <select
      id="theme-picker"
      title="Colour scheme"
      aria-label="Colour scheme"
      value={theme()}
      onChange={(e) => applyTheme(e.currentTarget.value)}
    >
      <For each={Object.entries(THEMES)}>{([name, spec]) => <option value={name}>{spec.label}</option>}</For>
    </select>
  );
}
