import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import path from 'path'

const apiTarget = process.env.VITE_API_TARGET || 'http://localhost:8173'
const systemApiTarget = process.env.VITE_SYSTEM_API_TARGET || apiTarget
// A feature preview can exercise a new preference API against its isolated
// backend while all live tabs, streams, statuses and workspace data continue
// to come from the main Hub. Production leaves this unset and uses apiTarget.
const sidebarPreferencesApiTarget =
  process.env.VITE_SIDEBAR_PREFERENCES_API_TARGET || apiTarget
const devPort = Number(process.env.VITE_PORT || process.env.PORT || 5173)
const buildOutDir = process.env.CLAUDE_HUB_FRONTEND_OUT_DIR || 'dist'

// The backend rejects browser Origin values that do not match the target Host
// (CSWSH / cross-site write protection). Vite is a trusted loopback proxy, so
// present the backend's own origin: changeOrigin rewrites Host and this
// rewrites Origin on both HTTP and WebSocket upgrade requests. Without it the
// dev UI's origin (:5173) differs from the proxied target (:8173) and the
// terminal WebSocket would be denied with close code 1008.
const proxyHeaders = (target: string): Record<string, string> => ({
  Origin: new URL(target).origin,
})

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
  build: {
    outDir: buildOutDir,
    emptyOutDir: true,
  },
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
      '/api/tabs/sidebar-preferences': {
        target: sidebarPreferencesApiTarget,
        changeOrigin: true,
        headers: proxyHeaders(sidebarPreferencesApiTarget),
      },
      '/api/system': {
        target: systemApiTarget,
        changeOrigin: true,
        headers: proxyHeaders(systemApiTarget),
      },
      '/api': {
        target: apiTarget,
        changeOrigin: true,
        ws: true,
        headers: proxyHeaders(apiTarget),
      },
    },
  },
})
