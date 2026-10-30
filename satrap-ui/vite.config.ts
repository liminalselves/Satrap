import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import fs from 'fs';
import path from 'path';

/** 前后端共用 satrap/_version.py 的版本号, 不在前端另写一份 */
function appVersion(): string {
  const text = fs.readFileSync(path.resolve(__dirname, '../satrap/_version.py'), 'utf-8');
  const match = text.match(/^__version__\s*=\s*"([^"]+)"/m);
  if (!match) throw new Error('satrap/_version.py 中没有 __version__');
  return match[1];
}

export default defineConfig({
  plugins: [react()],
  define: {
    __APP_VERSION__: JSON.stringify(appVersion()),
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 5173,
    // 用户文档在仓库 docs/user, 位于前端根目录之外
    fs: { allow: ['.', '../docs/user'] },
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:19870',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://127.0.0.1:19870',
        ws: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ['react', 'react-dom', 'react-router-dom'],
          ui: ['lucide-react', 'clsx', 'tailwind-merge'],
        },
      },
    },
  },
});
