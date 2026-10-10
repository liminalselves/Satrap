import axios from 'axios';
import { ApiError } from '@/api/client';

type ErrorBody = { error?: string; detail?: string };

// 统一错误文案: ApiError 附稳定原因码, axios 错误优先取后端信封的 error/detail 字段,
// 其余 Error 取 message, 无法识别时回退到通用提示 (不再各页面重复实现)
export function errorText(error: unknown, fallback = '操作失败'): string {
  if (error instanceof ApiError) return error.code ? `${error.message} (${error.code})` : error.message;
  if (axios.isAxiosError<ErrorBody>(error)) {
    return error.response?.data?.error || error.response?.data?.detail || error.message;
  }
  if (error instanceof Error) return error.message;
  return typeof error === 'string' && error ? error : fallback;
}
