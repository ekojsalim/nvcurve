import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react-swc'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      '/api': 'http://localhost:8042',
      '/ws': { target: 'ws://localhost:8042', ws: true },
    },
    allowedHosts: ["localhost", ".localhost", ".ts.net"]
  },
})
