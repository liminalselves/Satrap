import { describe, expect, it } from 'vitest';
import { AxiosError } from 'axios';
import { administratorError } from './administrators';
import { loggingError } from './logging';
import { ragErrorMessage } from './rag';
import { ApiError } from './client';

describe('公共错误格式与领域提示', () => {
  it.each([administratorError, loggingError, ragErrorMessage])('保留后端 detail 和 ApiError 的稳定错误码', (format) => {
    const error = new AxiosError('Request failed');
    error.response = { status: 409, data: { detail: '版本已变化' }, headers: {}, statusText: '', config: { headers: {} } } as typeof error.response;
    expect(format(error)).toBe('版本已变化');
    expect(format(new ApiError('请刷新', 409, 'conflict'))).toBe('请刷新 (conflict)');
  });

  it('RAG 请求超时和网络失败仍说明任务结果未知', () => {
    expect(ragErrorMessage(new AxiosError('timeout', 'ETIMEDOUT'))).toContain('任务可能仍在执行');
    expect(ragErrorMessage(new AxiosError('offline', 'ERR_NETWORK'))).toContain('任务结果未知');
    expect(ragErrorMessage(123)).toBe('123');
  });
});
