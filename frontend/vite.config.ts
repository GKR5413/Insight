import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// In dev the SPA runs on :5173 and proxies /api to the FastAPI server, so there is no CORS to fight.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true } },
  },
})
