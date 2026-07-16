import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 库构建：把组件 + 非 React 依赖（three / @react-three/fiber / react-reconciler /
// scheduler / framer-motion / gsap）打进一个 ESM 产物，仅把 react / react-dom 外置。
// Claude Design /design-sync 的转换器再从 dist/lib/index.js 二次打包成 IIFE，
// 把外置的 react 解析到 window.React —— 这样转换器永远不会遇到裸 'scheduler' 导入。
export default defineConfig({
  plugins: [react()],
  build: {
    lib: {
      entry: 'src/index.ts',
      formats: ['es'],
      fileName: () => 'index.js',
    },
    outDir: 'dist/lib',
    emptyOutDir: true,
    minify: false,
    rollupOptions: {
      external: [
        'react',
        'react-dom',
        'react/jsx-runtime',
        'react/jsx-dev-runtime',
        'react-dom/client',
      ],
    },
  },
})
