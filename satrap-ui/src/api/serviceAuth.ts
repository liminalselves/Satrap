import type { AxiosError, AxiosInstance, InternalAxiosRequestConfig } from 'axios';
import { ensureApiSession, establishApiSession } from './auth';

export type ServiceRequestConfig = { _satrapBoundService?: string; _satrapCurrentService?: () => string; _satrapNoAuthReplay?: boolean };
type AuthRequestConfig = InternalAxiosRequestConfig & ServiceRequestConfig & { _satrapAuthRetry?: boolean };

interface AuthOptions<T> {
  currentService: () => string;
  resolveService?: (config: AuthRequestConfig) => string;
  verifyService?: (config: AuthRequestConfig, service: string) => void;
  canReplay?: (config: AuthRequestConfig) => boolean;
  unwrapResponse?: boolean;
  formatError?: (error: AxiosError<T>) => unknown;
}

// 共用会话建立与一次 401 重放, 地址绑定, 写请求重放策略和错误信封留给各客户端选择
export function configureServiceAuth<T = unknown>(client: AxiosInstance, options: AuthOptions<T>) {
  client.interceptors.request.use(async (config) => {
    const request = config as AuthRequestConfig;
    const service = options.resolveService ? options.resolveService(request)
      : request._satrapAuthRetry && config.baseURL ? config.baseURL : options.currentService();
    await ensureApiSession(service);
    options.verifyService?.(request, service);
    config.baseURL = service;
    return config;
  });
  client.interceptors.response.use(
    (response) => options.unwrapResponse ? response.data : response,
    async (error: AxiosError<T>) => {
      const request = error.config as AuthRequestConfig | undefined;
      if (error.response?.status === 401 && request && !request._satrapAuthRetry && (options.canReplay?.(request) ?? true)) {
        request._satrapAuthRetry = true;
        await establishApiSession(request.baseURL || options.currentService());
        return client.request(request);
      }
      return Promise.reject(options.formatError ? options.formatError(error) : error);
    },
  );
}
