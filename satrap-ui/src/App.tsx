import { lazy, Suspense, useEffect, type ReactElement } from 'react';
import { Routes, Route } from 'react-router-dom';
import { AppLayout } from '@/components/layout/AppLayout';

const Dashboard = lazy(() => import('@/pages/Dashboard').then((module) => ({ default: module.Dashboard })));
const Models = lazy(() => import('@/pages/Models').then((module) => ({ default: module.Models })));
const Rag = lazy(() => import('@/pages/Rag').then((module) => ({ default: module.Rag })));
const Sessions = lazy(() => import('@/pages/Sessions').then((module) => ({ default: module.Sessions })));
const Platforms = lazy(() => import('@/pages/Platforms').then((module) => ({ default: module.Platforms })));
const Logs = lazy(() => import('@/pages/Logs').then((module) => ({ default: module.Logs })));
const Checkpoints = lazy(() => import('@/pages/Checkpoints').then((module) => ({ default: module.Checkpoints })));
const Users = lazy(() => import('@/pages/Users').then((module) => ({ default: module.Users })));
const Settings = lazy(() => import('@/pages/Settings').then((module) => ({ default: module.Settings })));
const Chat = lazy(() => import('@/pages/Chat').then((module) => ({ default: module.Chat })));

function lazyRoute(element: ReactElement) {
  return (
    <Suspense fallback={<div className="min-h-[40vh]" aria-label="页面加载中" />}>
      {element}
    </Suspense>
  );
}

function App() {
  useEffect(() => {
    const syncBackgroundVisibility = () => {
      document.documentElement.classList.toggle('background-animation-paused', document.hidden);
    };
    syncBackgroundVisibility();
    document.addEventListener('visibilitychange', syncBackgroundVisibility);
    return () => {
      document.removeEventListener('visibilitychange', syncBackgroundVisibility);
      document.documentElement.classList.remove('background-animation-paused');
    };
  }, []);

  return (
    <>
      {/* 柔和流体背景 - 8个球体 */}
      <div className="animated-bg">
        <div className="fluid-blob fluid-blob-1" />
        <div className="fluid-blob fluid-blob-2" />
        <div className="fluid-blob fluid-blob-3" />
        <div className="fluid-blob fluid-blob-4" />
        <div className="fluid-blob fluid-blob-5" />
        <div className="fluid-blob fluid-blob-6" />
        <div className="fluid-blob fluid-blob-7" />
        <div className="fluid-blob fluid-blob-8" />
      </div>
      
      <Routes>
        {/* 聊天页: 完全独立整页, 不渲染管理面板布局 */}
        <Route path="/chat" element={lazyRoute(<Chat />)} />

        <Route element={<AppLayout />}>
          <Route path="/" element={lazyRoute(<Dashboard />)} />
          <Route path="/models" element={lazyRoute(<Models />)} />
          <Route path="/rag" element={lazyRoute(<Rag />)} />
          <Route path="/sessions" element={lazyRoute(<Sessions />)} />
          <Route path="/platforms" element={lazyRoute(<Platforms />)} />
          <Route path="/logs" element={lazyRoute(<Logs />)} />
          <Route path="/checkpoints" element={lazyRoute(<Checkpoints />)} />
          <Route path="/users" element={lazyRoute(<Users />)} />
          <Route path="/settings" element={lazyRoute(<Settings />)} />
        </Route>
      </Routes>
    </>
  );
}

export default App;
