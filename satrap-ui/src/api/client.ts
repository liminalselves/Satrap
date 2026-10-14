import axios, { AxiosInstance, AxiosError, AxiosRequestConfig } from 'axios';
import { getApiBaseUrl } from '@/utils/constants';
import { configureServiceAuth, type ServiceRequestConfig } from './serviceAuth';

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
export function toApiError(error: AxiosError<ErrorBody> | ApiError): ApiError {
  if (error instanceof ApiError) return error;
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

    configureServiceAuth<ErrorBody>(this.client, {
      currentService: getApiBaseUrl,
      resolveService: (config) => config._satrapBoundService || (config._satrapAuthRetry && config.baseURL ? config.baseURL : getApiBaseUrl()),
      verifyService: (config, service) => {
        if (config._satrapBoundService && (config._satrapCurrentService || getApiBaseUrl)().replace(/\/$/, '') !== service) {
          throw new ApiError('后端地址已变化, 原应用任务的生效状态未确认', undefined, 'service_changed');
        }
      },
      canReplay: (config) => !config._satrapNoAuthReplay,
      unwrapResponse: true,
      formatError: toApiError,
    });
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

  forService(baseUrl: string, currentService = getApiBaseUrl) {
    const bound = baseUrl.replace(/\/$/, '');
    const request = <T>(method: 'GET' | 'POST' | 'PUT', url: string, data: unknown, timeout: number, signal?: AbortSignal): Promise<T> => {
      const config: AxiosRequestConfig & ServiceRequestConfig = {
        method, url, data, signal, timeout, baseURL: bound,
        _satrapBoundService: bound, _satrapCurrentService: currentService, _satrapNoAuthReplay: method !== 'GET',
      };
      return this.client.request(config) as Promise<T>;
    };
    return {
      get: <T>(url: string, signal?: AbortSignal, timeout = 10000) => request<T>('GET', url, undefined, timeout, signal),
      post: <T>(url: string, data: unknown, signal?: AbortSignal, timeout = 120000) => request<T>('POST', url, data, timeout, signal),
      put: <T>(url: string, data: unknown, signal?: AbortSignal, timeout = 120000) => request<T>('PUT', url, data, timeout, signal),
    };
  }
}

export const apiClient = new ApiClient();
