import axios from 'axios';
import { CONTROL_API_URL } from '@/utils/constants';
import type { PlatformConfig } from './types';

// 后端控制 API 客户端(独立于主后端)
const controlClient = axios.create({
  baseURL: CONTROL_API_URL,
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
});

export interface BackendStatus {
  running: boolean;
  managed: boolean;
  health?: {
    running: boolean;
    adapters?: Record<string, unknown>;
    sessions?: number;
    users?: number;
  };
}

export interface ControlResult {
  ok: boolean;
  message?: string;
  error?: string;
}

export interface ConfigResult {
  ok: boolean;
  config?: Record<string, unknown>;
  path?: string;
  exists?: boolean;
  message?: string;
  error?: string;
}

export interface PlatformConfigResult extends ControlResult {
  platforms?: PlatformConfig[];
  exists?: boolean;
}

export const controlApi = {
  // 获取后端状态
  status: async (): Promise<BackendStatus> => {
    const response = await controlClient.get<BackendStatus>('/status');
    return response.data;
  },

  // 启动后端
  start: async (): Promise<ControlResult> => {
    const response = await controlClient.post<ControlResult>('/start');
    return response.data;
  },

  // 停止后端
  stop: async (): Promise<ControlResult> => {
    const response = await controlClient.post<ControlResult>('/stop');
    return response.data;
  },

  // 重启后端
  restart: async (): Promise<ControlResult> => {
    const response = await controlClient.post<ControlResult>('/restart');
    return response.data;
  },

  // 读取配置文件
  getConfig: async (): Promise<ConfigResult> => {
    const response = await controlClient.get<ConfigResult>('/config');
    return response.data;
  },

  // 保存配置文件
  saveConfig: async (config: Record<string, unknown>): Promise<ConfigResult> => {
    const response = await controlClient.put<ConfigResult>('/config', config);
    return response.data;
  },

  // 创建默认配置文件
  createDefaultConfig: async (): Promise<ConfigResult> => {
    const response = await controlClient.post<ConfigResult>('/config/default');
    return response.data;
  },

  // 校验配置文件
  validateConfig: async (config: Record<string, unknown>): Promise<ConfigResult> => {
    const response = await controlClient.post<ConfigResult>('/config/validate', config);
    return response.data;
  },

  // 读取平台配置
  listPlatforms: async (): Promise<PlatformConfigResult> => {
    const response = await controlClient.get<PlatformConfigResult>('/config/platforms');
    return response.data;
  },

  // 创建平台配置
  createPlatform: async (platform: PlatformConfig): Promise<PlatformConfigResult> => {
    const response = await controlClient.post<PlatformConfigResult>('/config/platforms', platform);
    return response.data;
  },

  // 更新平台配置
  updatePlatform: async (originalId: string, platform: PlatformConfig): Promise<PlatformConfigResult> => {
    const response = await controlClient.put<PlatformConfigResult>(
      `/config/platforms/${encodeURIComponent(originalId)}`,
      platform,
    );
    return response.data;
  },

  // 删除平台配置
  deletePlatform: async (platformId: string): Promise<PlatformConfigResult> => {
    const response = await controlClient.delete<PlatformConfigResult>(
      `/config/platforms/${encodeURIComponent(platformId)}`,
    );
    return response.data;
  },
};
