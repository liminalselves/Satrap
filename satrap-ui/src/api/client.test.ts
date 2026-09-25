import { describe, expect, it } from 'vitest';
import { AxiosError, type AxiosResponse } from 'axios';
import { ApiError, toApiError } from './client';

function responseError(status: number, data: unknown): AxiosError<{ error?: string; reason?: string }> {
  const response = { status, data } as AxiosResponse;
  return new AxiosError('Request failed with status code ' + status, 'ERR_BAD_RESPONSE', undefined, undefined, response);
}

describe('API 错误原因码', () => {
  it('保留后端拒绝信封的稳定原因码', () => {
    const error = toApiError(responseError(409, { status: 'rejected', request_id: 'r1', reason: 'queue_full' }));
    expect(error).toBeInstanceOf(ApiError);
    expect(error.code).toBe('queue_full');
    expect(error.status).toBe(409);
    // 后端不给 error 字段时消息保持 axios 原文, 不虚构原因
    expect(error.message).toBe('Request failed with status code 409');
  });

  it('error 字段仍优先作为消息', () => {
    const error = toApiError(responseError(400, { status: 'rejected', error: '请求体不是合法 JSON' }));
    expect(error.message).toBe('请求体不是合法 JSON');
    expect(error.code).toBeUndefined();
  });

  it('非对象响应体与网络错误不产生原因码', () => {
    expect(toApiError(responseError(502, '<html>Bad gateway</html>')).code).toBeUndefined();
    const network = toApiError(new AxiosError('Network Error', 'ERR_NETWORK'));
    expect(network.status).toBeUndefined();
    expect(network.code).toBeUndefined();
    expect(network.message).toBe('Network Error');
  });

  it('reason 不是非空字符串时按无原因码处理', () => {
    expect(toApiError(responseError(409, { reason: 42 })).code).toBeUndefined();
    expect(toApiError(responseError(409, { reason: '' })).code).toBeUndefined();
  });
});
