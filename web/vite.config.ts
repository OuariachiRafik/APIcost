import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
// `defineConfig` comes from vitest/config, not vite, so the `test` block below
// is type-checked rather than rejected as an unknown property.
import { defineConfig } from 'vitest/config';

export default defineConfig({
  plugins: [react(), tailwindcss()],
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
