import axios, { AxiosInstance, AxiosError, InternalAxiosRequestConfig } from 'axios';
import { getApiBaseUrl } from '@/utils/constants';
import { establishApiSession } from '@/api/auth';

type RetriableRequestConfig = InternalAxiosRequestConfig & { _satrapAuthRetry?: boolean };

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

    this.client.interceptors.request.use((config) => {
      config.baseURL = getApiBaseUrl();
      return config;
    });

    this.setupInterceptors();
  }

  private setupInterceptors() {
    this.client.interceptors.response.use(
      (response) => response.data,
      async (error: AxiosError<{ error?: string }>) => {
        const config = error.config as RetriableRequestConfig | undefined;
        if (error.response?.status === 401 && config && !config._satrapAuthRetry) {
          config._satrapAuthRetry = true;
          await establishApiSession(getApiBaseUrl());
          return this.client.request(config);
        }
        const message = error.response?.data?.error || error.message;
        const status = error.response?.status;
        return Promise.reject(new ApiError(message, status));
      }
    );
  }

  async get<T>(url: string, params?: Record<string, unknown>): Promise<T> {
    return this.client.get(url, { params }) as Promise<T>;
  }

  async post<T>(url: string, data?: unknown): Promise<T> {
    return this.client.post(url, data) as Promise<T>;
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
}

export const apiClient = new ApiClient();
