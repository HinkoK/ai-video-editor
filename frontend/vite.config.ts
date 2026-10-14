import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// В разработке запросы /api уходят на локальный сервер редактора. Сервер
// пускает только свой Origin, поэтому для npm run dev его запускают с
// LONGFORM_DEV_ORIGIN=http://localhost:5173.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { '/api': { target: 'http://127.0.0.1:8765', changeOrigin: true } },
  },
  test: { environment: 'node' },
})
