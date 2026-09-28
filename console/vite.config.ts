import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 构建成相对路径的静态资源，供独立 RSI 服务内嵌。
export default defineConfig({
  plugins: [react()],
  base: './',
  build: { outDir: 'dist', assetsDir: 'assets', target: 'es2020' },
  server: { port: 5180 },
})
