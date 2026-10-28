import { useEffect } from 'react';
import { RouterProvider } from 'react-router-dom';
import { router } from './router';
import { useBackendStatusSync } from './hooks/useBackendStatusSync';
import { ApiSessionNotice } from '@/components/common/ApiSessionNotice';
import { PasswordGate } from '@/components/common/PasswordGate';
import { ConfirmProvider } from '@/components/common/ConfirmProvider';
import { ToastContainer, useToasts } from '@/components/ui/Toast';
import { usePrefsStore, useVisualEffects } from '@/stores/usePrefsStore';
import { startReflectEngine } from '@/hooks/reflectEngine';

// 类名必须完整写出: Tailwind 按源码文本保留 @layer 内的规则, 拼接出的类名会被裁掉
const BLOB_CLASSES = ['fluid-blob-1', 'fluid-blob-2', 'fluid-blob-3', 'fluid-blob-4', 'fluid-blob-5', 'fluid-blob-6', 'fluid-blob-7', 'fluid-blob-8'];

function App() {
  const { toasts, closeToast } = useToasts();
  const motion = usePrefsStore((state) => state.motion);
  useBackendStatusSync();
  const { glass, ambient } = useVisualEffects();
  // 没有悬停指针的设备看不到反光, 不启动引擎
  useEffect(() => (window.matchMedia?.('(hover: none)').matches ? undefined : startReflectEngine()), []);
  useEffect(() => { document.documentElement.setAttribute('data-motion', motion); }, [motion]);
  useEffect(() => { document.documentElement.setAttribute('data-glass', glass ? 'on' : 'off'); }, [glass]);
  useEffect(() => { document.documentElement.setAttribute('data-ambient', ambient ? 'on' : 'off'); }, [ambient]);
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
      <div className="animated-bg">
        {ambient && BLOB_CLASSES.map((name) => <div key={name} className={`fluid-blob ${name}`} />)}
      </div>

      <ConfirmProvider><RouterProvider router={router} /></ConfirmProvider>
      <ApiSessionNotice />
      <ToastContainer toasts={toasts} onClose={closeToast} />
      <PasswordGate />
    </>
  );
}

export default App;
