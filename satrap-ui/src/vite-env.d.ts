/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string;
  readonly VITE_CONTROL_API_URL?: string;
  readonly VITE_CHAT_API_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

/** 构建时由 vite.config.ts 从 satrap/_version.py 注入 */
declare const __APP_VERSION__: string;
