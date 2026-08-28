import { Show, createEffect, onMount } from 'solid-js';

import { Sidebar } from './components/Sidebar';
import { SidePanel } from './components/SidePanel';
import { TerminalLayout } from './components/TerminalLayout';
import { TopBar } from './components/TopBar';
import { sidePanelEnabled } from './sidePanel';
import { init, sidebarHidden } from './store';

export function App() {
  onMount(() => {
    init().catch((err) => console.error('could not load the workbench', err));
  });

  createEffect(() => document.body.classList.toggle('sidebar-hidden', sidebarHidden()));

  return (
    <>
      <Sidebar />
      <div id="main">
        <TopBar />
        <div id="sp-split">
          <div id="sp-left">
            <TerminalLayout />
          </div>
          <Show when={sidePanelEnabled}>
            <SidePanel />
          </Show>
        </div>
      </div>
    </>
  );
}
