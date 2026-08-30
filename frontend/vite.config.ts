import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Proxy /api and /ws to the FastAPI backend (uvicorn backend.main:app --port 8000)
// so the frontend can call same-origin paths in dev without worrying about CORS.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://localhost:8000',
        ws: true,
      },
    },
  },
})
