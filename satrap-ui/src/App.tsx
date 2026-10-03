import { useEffect } from 'react';
import { RouterProvider } from 'react-router-dom';
import { router } from './router';
import { useBackendStatusSync } from './hooks/useBackendStatusSync';

function App() {
  useBackendStatusSync();
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
      
      <RouterProvider router={router} />
    </>
  );
}

export default App;
