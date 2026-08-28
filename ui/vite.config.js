import { defineConfig } from 'vite';
import solid from 'vite-plugin-solid';

// Built assets land in the directory Litestar serves as /static/ui, under fixed names: the page is
// a Jinja template that references them by name, and the server sends no-store for /static.
export default defineConfig({
  plugins: [solid()],
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
