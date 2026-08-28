/** Copy to the system clipboard when the terminal emits an OSC 52 sequence. */
export function handleOsc52(chunk) {
  const s = new TextDecoder('latin1').decode(chunk);
  const m = s.match(/\x1b\]52;[^;]*;([A-Za-z0-9+/=]+)(?:\x07|\x1b\\)/);
  if (!m || m[1] === '?') return;
  try {
    const raw = atob(m[1]);
    const bytes = Uint8Array.from(raw, (c) => c.charCodeAt(0));
    navigator.clipboard.writeText(new TextDecoder().decode(bytes)).catch(() => {});
  } catch (_) {}
}

/** Claude Code prints a verbose "sent N chars via OSC 52 · if paste fails…" hint because it can't
 *  confirm the clipboard write succeeded. We just did it, so show the short form instead. */
export function filterOsc52Noise(chunk) {
  const s = new TextDecoder('latin1').decode(chunk);
  const filtered = s.replace(/sent (\d+) chars? via OSC 52[^\r\n]*/g, 'copied $1 chars to clipboard');
  if (filtered === s) return chunk;
  return Uint8Array.from(filtered, (c) => c.charCodeAt(0));
}
