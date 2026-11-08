import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

let port = 8000
try {
  const config = JSON.parse(readFileSync(resolve(process.env.LAXCODE_CONFIG ?? '../config/config.json'), 'utf8'))
  port = config.api_server.port
} catch { /* Backend-only development can use the example port. */ }

export default defineConfig({
  plugins: [react()],
  server: { proxy: { '/api': { target: `http://127.0.0.1:${port}` } } },
  test: { environment: 'jsdom', include: ['tests/**/*.test.{ts,tsx}'], setupFiles: ['tests/setup.ts'] },
})
