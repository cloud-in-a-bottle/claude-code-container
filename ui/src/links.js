import { WebLinksAddon } from '@xterm/addon-web-links';

/** Cmd on macOS, Ctrl everywhere else -- on a Mac ctrl-click is a right-click, so it can't be the
 *  one that follows links. */
const IS_MAC = /mac|iphone|ipad/i.test(navigator.platform || navigator.userAgent);
const MODIFIER_LABEL = IS_MAC ? '⌘' : 'Ctrl';

const modifierHeld = (event) => (IS_MAC ? event.metaKey : event.ctrlKey);

/** Only ever follow http(s). The text of a link is chosen by whatever program is running in the
 *  terminal, so `javascript:` would otherwise hand it a way to run script in the workbench page. */
const followable = (uri) => /^https?:\/\//i.test(uri);

/** Link support for one terminal: URLs printed as plain text and OSC 8 hyperlinks both open in a new
 *  browser tab on modifier-click, the way iTerm2 and VS Code do it. A plain click is left alone,
 *  because in a terminal you click to place the cursor and drag to select.
 *
 *  Nothing about the modifier is discoverable on its own, so `showHint` is called with a line to
 *  display while a link is under the pointer: it names the key and shows where the link actually
 *  goes, which for an OSC 8 hyperlink is not what the text says.
 *
 *  `host` only takes a class while the modifier is down, for the cursor styling in layout.css. */
export function createLinkSupport(host, showHint) {
  function hover(event, uri) {
    if (!followable(uri)) return;
    showHint(`${MODIFIER_LABEL}-click to open ${uri}`);
  }

  function leave() {
    showHint('');
  }

  function activate(event, uri) {
    if (!modifierHeld(event) || !followable(uri)) return;
    window.open(uri, '_blank', 'noopener,noreferrer');
  }

  const trackModifier = (e) => host.classList.toggle('link-modifier', modifierHeld(e));
  // Cmd-tabbing away never delivers the keyup, which would otherwise leave the class stuck on.
  const clearModifier = () => host.classList.remove('link-modifier');
  window.addEventListener('keydown', trackModifier);
  window.addEventListener('keyup', trackModifier);
  window.addEventListener('blur', clearModifier);

  return {
    // OSC 8 hyperlinks. xterm matches these itself, since the program marked them up explicitly.
    linkHandler: { activate, hover, leave },
    // Bare URLs, which the addon finds by scanning the buffer -- including ones wrapped over lines.
    addon: new WebLinksAddon(activate, { hover, leave }),
    dispose() {
      window.removeEventListener('keydown', trackModifier);
      window.removeEventListener('keyup', trackModifier);
      window.removeEventListener('blur', clearModifier);
      showHint('');
    },
  };
}
