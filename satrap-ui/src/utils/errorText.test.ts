import { describe, expect, it } from 'vitest';
import { AxiosError, type InternalAxiosRequestConfig } from 'axios';
import { ApiError } from '@/api/client';
import { errorText } from './errorText';

function axiosError(body: unknown, message = 'Request failed'): AxiosError {
  const error = new AxiosError(message);
  error.config = { headers: {} } as InternalAxiosRequestConfig;
  error.response = { data: body, status: 400, statusText: 'Bad Request', headers: {}, config: error.config };
  return error;
}

describe('errorText', () => {
  it('ApiError 携带原因码时追加码值', () => {
    expect(errorText(new ApiError('保存失败', 409, 'conflict'))).toBe('保存失败 (conflict)');
    expect(errorText(new ApiError('保存失败', 500))).toBe('保存失败');
  });

  it('axios 错误优先取后端信封的 error, 其次 detail, 最后 axios 自身 message', () => {
    expect(errorText(axiosError({ error: '参数无效', detail: '更细的说明' }))).toBe('参数无效');
    expect(errorText(axiosError({ detail: '更细的说明' }))).toBe('更细的说明');
    expect(errorText(axiosError(undefined, 'Network Error'))).toBe('Network Error');
  });

  it('普通 Error 取 message, 字符串原样返回', () => {
    expect(errorText(new Error('读取失败'))).toBe('读取失败');
    expect(errorText('直接的错误串')).toBe('直接的错误串');
  });

  it('无法识别的值回退到通用提示, 支持自定义兜底', () => {
    expect(errorText({ unexpected: true })).toBe('操作失败');
    expect(errorText(null, '请求失败')).toBe('请求失败');
  });
});
