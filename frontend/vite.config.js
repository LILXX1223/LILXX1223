import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  server: { host: '127.0.0.1', port: Number(process.env.VITE_PORT || 5173), strictPort: true, proxy: { '/api': { target: process.env.VITE_API_TARGET || 'http://127.0.0.1:8000', changeOrigin: true }, '/admin': { target: process.env.VITE_API_TARGET || 'http://127.0.0.1:8000', changeOrigin: true }, '/static': { target: process.env.VITE_API_TARGET || 'http://127.0.0.1:8000', changeOrigin: true } } },
})
