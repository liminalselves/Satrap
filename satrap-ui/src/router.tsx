import { lazy, Suspense, type ReactElement } from 'react';
import { createBrowserRouter } from 'react-router-dom';
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

// 数据路由: 只有数据路由才支持 useBlocker, 浏览器前进/后退与站内导航才能统一经过离开确认
export const router = createBrowserRouter([
  // 聊天页: 完全独立整页, 不渲染管理面板布局
  { path: '/chat', element: lazyRoute(<Chat />) },
  {
    element: <AppLayout />,
    children: [
      { index: true, element: lazyRoute(<Dashboard />) },
      { path: 'models', element: lazyRoute(<Models />) },
      { path: 'rag', element: lazyRoute(<Rag />) },
      { path: 'sessions', element: lazyRoute(<Sessions />) },
      { path: 'platforms', element: lazyRoute(<Platforms />) },
      { path: 'logs', element: lazyRoute(<Logs />) },
      { path: 'checkpoints', element: lazyRoute(<Checkpoints />) },
      { path: 'users', element: lazyRoute(<Users />) },
      { path: 'settings', element: lazyRoute(<Settings />) },
    ],
  },
  // 未匹配路径保持与旧结构一致的空渲染, 不进入管理布局
  { path: '*', element: null },
]);
