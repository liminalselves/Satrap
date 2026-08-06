import axios, { AxiosInstance, AxiosError } from 'axios';
import { API_BASE_URL } from '@/utils/constants';

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
      baseURL: API_BASE_URL,
      timeout: 10000,
      headers: {
        'Content-Type': 'application/json',
      },
    });

    this.setupInterceptors();
  }

  private setupInterceptors() {
    this.client.interceptors.response.use(
      (response) => response.data,
      (error: AxiosError<{ error?: string }>) => {
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
