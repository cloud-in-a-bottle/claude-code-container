import { defineConfig } from 'vite';
import solid from 'vite-plugin-solid';

/** Measure glyphs in xterm's DOM renderer to the subpixel, not to the whole pixel.
 *
 *  xterm sizes a glyph by laying out 32 of them and dividing `offsetWidth` by 32, but offsetWidth
 *  is rounded to an integer: Monaco at 17px is 10.2px a glyph, and 326 / 32 says 10.1875. The
 *  renderer then pads every glyph with letter-spacing to make up the difference to the cell, so
 *  each one comes out wider than its cell -- and Firefox rounds that padding up to its 1/60px
 *  layout unit -- until a wide terminal's last column runs a few pixels past the row and gets
 *  clipped. Still there in 6.1's betas. The build fails if the line ever changes, so an xterm
 *  upgrade can't drop the fix silently. */
function xtermSubpixelGlyphWidth() {
  const measure = 'return i.textContent=t.repeat(32),i.offsetWidth/32';
  return {
    name: 'xterm-subpixel-glyph-width',
    transform(code, id) {
      if (!/[\\/]@xterm[\\/]xterm[\\/]lib[\\/]xterm\.mjs$/.test(id)) return null;
      if (!code.includes(measure)) {
        throw new Error('xterm-subpixel-glyph-width: WidthCache._measure has changed; recheck the patch');
      }
      return code.replace(measure, 'return i.textContent=t.repeat(32),i.getBoundingClientRect().width/32');
    },
  };
}

// Built assets land in the directory Litestar serves as /static/ui, under fixed names: the page is
// a Jinja template that references them by name, and the server sends no-store for /static.
export default defineConfig({
  plugins: [solid(), xtermSubpixelGlyphWidth()],
  build: {
    outDir: '../src/server/static/ui',
    emptyOutDir: true,
    rollupOptions: {
      input: 'src/main.jsx',
      output: {
        entryFileNames: 'bundle.js',
        chunkFileNames: '[name].js',
        assetFileNames: 'bundle.[ext]',
      },
    },
  },
});
