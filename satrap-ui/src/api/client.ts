import axios, { AxiosInstance, AxiosError, AxiosRequestConfig, InternalAxiosRequestConfig } from 'axios';
import { getApiBaseUrl } from '@/utils/constants';
import { ensureApiSession, establishApiSession } from '@/api/auth';

type ServiceRequestConfig = { _satrapBoundService?: string; _satrapNoAuthReplay?: boolean };
type RetriableRequestConfig = InternalAxiosRequestConfig & ServiceRequestConfig & { _satrapAuthRetry?: boolean };
type ErrorBody = { error?: string; reason?: string };

export class ApiError extends Error {
  constructor(
    message: string,
    public status?: number,
    public code?: string
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

// 后端拒绝统一用 {status, reason} 信封, error 只用于请求体解析失败等通用错误;
// 保留稳定原因码, 调用方据此区分队列满/冲突/存储不可用, 而不是只看 HTTP 状态
export function toApiError(error: AxiosError<ErrorBody>): ApiError {
  const data = error.response?.data;
  const body = data && typeof data === 'object' ? data : undefined;
  const message = body?.error || error.message;
  const code = typeof body?.reason === 'string' && body.reason ? body.reason : undefined;
  return new ApiError(message, error.response?.status, code);
}

class ApiClient {
  private client: AxiosInstance;

  constructor() {
    this.client = axios.create({
      timeout: 10000,
      withCredentials: true,
      headers: {
        'Content-Type': 'application/json',
      },
    });

    this.client.interceptors.request.use(async (config) => {
      const retry = config as RetriableRequestConfig;
      const baseUrl = retry._satrapBoundService || (retry._satrapAuthRetry && config.baseURL ? config.baseURL : getApiBaseUrl());
      await ensureApiSession(baseUrl);
      if (retry._satrapBoundService && getApiBaseUrl().replace(/\/$/, '') !== baseUrl) {
        throw new ApiError('后端地址已变化, 原应用任务的生效状态未确认', undefined, 'service_changed');
      }
      config.baseURL = baseUrl;
      return config;
    });

    this.setupInterceptors();
  }

  private setupInterceptors() {
    this.client.interceptors.response.use(
      (response) => response.data,
      async (error: AxiosError<ErrorBody>) => {
        const config = error.config as RetriableRequestConfig | undefined;
        if (error.response?.status === 401 && config && !config._satrapAuthRetry && !config._satrapNoAuthReplay) {
          config._satrapAuthRetry = true;
          await establishApiSession(config.baseURL || getApiBaseUrl());
          return this.client.request(config);
        }
        return Promise.reject(toApiError(error));
      }
    );
  }

  async get<T>(url: string, params?: Record<string, unknown>, signal?: AbortSignal): Promise<T> {
    return this.client.get(url, { params, signal }) as Promise<T>;
  }

  async post<T>(url: string, data?: unknown, timeout?: number): Promise<T> {
    return this.client.post(url, data, timeout === undefined ? undefined : { timeout }) as Promise<T>;
  }

  async put<T>(url: string, data?: unknown): Promise<T> {
    return this.client.put(url, data) as Promise<T>;
  }

  async patch<T>(url: string, data?: unknown): Promise<T> {
    return this.client.patch(url, data) as Promise<T>;
  }

  async delete<T>(url: string): Promise<T> {
    return this.client.delete(url) as Promise<T>;
  }

  forService(baseUrl: string) {
    const bound = baseUrl.replace(/\/$/, '');
    const request = <T>(method: 'GET' | 'POST', url: string, data: unknown, timeout: number, signal?: AbortSignal): Promise<T> => {
      const config: AxiosRequestConfig & ServiceRequestConfig = {
        method, url, data, signal, timeout, baseURL: bound,
        _satrapBoundService: bound, _satrapNoAuthReplay: method === 'POST',
      };
      return this.client.request(config) as Promise<T>;
    };
    return {
      get: <T>(url: string, signal?: AbortSignal, timeout = 10000) => request<T>('GET', url, undefined, timeout, signal),
      post: <T>(url: string, data: unknown, signal?: AbortSignal, timeout = 120000) => request<T>('POST', url, data, timeout, signal),
    };
  }
}

export const apiClient = new ApiClient();
