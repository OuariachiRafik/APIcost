import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
// `defineConfig` comes from vitest/config, not vite, so the `test` block below
// is type-checked rather than rejected as an unknown property.
import { defineConfig } from 'vitest/config';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    rollupOptions: {
      output: {
        // Recharts is ~60% of the bundle and changes far less often than the
        // app does. Splitting it means a deploy invalidates the app chunk
        // without making every user re-download the charting library.
        manualChunks: {
          charts: ['recharts'],
          vendor: ['react', 'react-dom', 'react-router-dom', '@tanstack/react-query'],
        },
      },
    },
  },
  server: {
    port: 5173,
    host: true, // reachable from outside the container
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/setupTests.ts'],
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
  },
});
