import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import './styles/globals.css';
import { loadRuntimeConfig } from '@/utils/constants';
import { GlassReflectProvider } from '@/hooks/useGlassReflect';
import { establishApiSessions } from '@/api/auth';

async function bootstrap() {
  const root = ReactDOM.createRoot(document.getElementById('root')!);
  root.render(<div role="status" className="flex min-h-screen items-center justify-center text-text-secondary">正在读取服务配置…</div>);
  await loadRuntimeConfig();
  void establishApiSessions(); // 各服务独立建立会话, 不阻塞页面外壳
  root.render(
    <React.StrictMode>
      <GlassReflectProvider>
        <App />
      </GlassReflectProvider>
    </React.StrictMode>
  );
}

void bootstrap();
