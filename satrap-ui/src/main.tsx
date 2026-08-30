import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import App from './App';
import './styles/globals.css';
import { loadRuntimeConfig } from '@/utils/constants';
import { GlassReflectProvider } from '@/hooks/useGlassReflect';

async function bootstrap() {
  await loadRuntimeConfig();
  ReactDOM.createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
      <GlassReflectProvider>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </GlassReflectProvider>
    </React.StrictMode>
  );
}

void bootstrap();
