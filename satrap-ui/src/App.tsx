import { Routes, Route } from 'react-router-dom';
import { AppLayout } from '@/components/layout/AppLayout';
import { Dashboard } from '@/pages/Dashboard';
import { Models } from '@/pages/Models';
import { Sessions } from '@/pages/Sessions';
import { Platforms } from '@/pages/Platforms';
import { Logs } from '@/pages/Logs';
import { Checkpoints } from '@/pages/Checkpoints';
import { Users } from '@/pages/Users';
import { Settings } from '@/pages/Settings';
import { Chat } from '@/pages/Chat';

function App() {
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
        <Route path="/chat" element={<Chat />} />

        <Route element={<AppLayout />}>
          <Route path="/" element={<Dashboard />} />
          <Route path="/models" element={<Models />} />
          <Route path="/sessions" element={<Sessions />} />
          <Route path="/platforms" element={<Platforms />} />
          <Route path="/logs" element={<Logs />} />
          <Route path="/checkpoints" element={<Checkpoints />} />
          <Route path="/users" element={<Users />} />
          <Route path="/settings" element={<Settings />} />
        </Route>
      </Routes>
    </>
  );
}

export default App;
