import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import path from 'path'

const apiTarget = process.env.VITE_API_TARGET || 'http://localhost:8173'
const systemApiTarget = process.env.VITE_SYSTEM_API_TARGET || apiTarget
const devPort = Number(process.env.VITE_PORT || process.env.PORT || 5173)

/**
 * SharedArrayBuffer (used by the terminal input fast path) requires the
 * top-level document to be cross-origin-isolated. That means COOP + COEP
 * headers must be present on every document response, including ones served
 * directly by Vite during development.
 */
function coopCoepHeadersPlugin(): import('vite').Plugin {
  return {
    name: 'coop-coep-headers',
    configureServer(server) {
      server.middlewares.use((_req, res, next) => {
        res.setHeader('Cross-Origin-Opener-Policy', 'same-origin')
        res.setHeader('Cross-Origin-Embedder-Policy', 'require-corp')
        res.setHeader('Cross-Origin-Resource-Policy', 'same-origin')
        next()
      })
    },
    configurePreviewServer(server) {
      server.middlewares.use((_req, res, next) => {
        res.setHeader('Cross-Origin-Opener-Policy', 'same-origin')
        res.setHeader('Cross-Origin-Embedder-Policy', 'require-corp')
        res.setHeader('Cross-Origin-Resource-Policy', 'same-origin')
        next()
      })
    },
  }
}

export default defineConfig({
  plugins: [vue(), coopCoepHeadersPlugin()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    host: '0.0.0.0',
    port: devPort,
    strictPort: true,
    allowedHosts: true,
    proxy: {
      '/api/system': {
        target: systemApiTarget,
        changeOrigin: true,
      },
      '/api': {
        target: apiTarget,
        changeOrigin: true,
        ws: true,
      },
    },
  },
})
