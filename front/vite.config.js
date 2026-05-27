import { defineConfig } from 'vite';

export default defineConfig({
  root: '.',
  publicDir: 'public',
  resolve: {
    alias: {
      // Alias pour importer trois/addons plus facilement
    }
  },
  server: {
    port: 3000,
    host: true
  },
  build: {
    outDir: 'dist',
    rollupOptions: {
      // Assure que duckdb-wasm est correctement bundlé
      external: []
    }
  }
});
