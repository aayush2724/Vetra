import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Proxying keeps the dashboard on one origin in development, so there is no
    // CORS story to get wrong and the same relative /api paths work in a build
    // served by the cloud service itself.
    proxy: {
      '/api': {
        target: process.env.VETRA_API ?? 'http://localhost:4000',
        changeOrigin: true,
      },
    },
  },
});
