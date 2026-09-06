import { describe, expect, it } from 'vitest';
import { AxiosError, type AxiosResponse } from 'axios';
import { ragErrorMessage } from './rag';

describe('RAG 错误显示', () => {
  it('保留后端的阶段和原始原因', () => {
    const response = { status: 500, data: { error: '向量化或构建索引失败: 服务返回 429: quota exceeded' } } as AxiosResponse;
    expect(ragErrorMessage(new AxiosError('Request failed', 'ERR_BAD_RESPONSE', undefined, undefined, response))).toBe(response.data.error);
  });

  it.each(['ECONNABORTED', 'ETIMEDOUT'])('超时 %s 提醒确认任务结果', (code) => {
    expect(ragErrorMessage(new AxiosError('timeout', code))).toContain('任务可能仍在执行');
  });

  it('网络中断不会虚构后端错误', () => {
    expect(ragErrorMessage(new AxiosError('Network Error', 'ERR_NETWORK'))).toContain('任务结果未知');
  });

  it('代理返回 HTML 时只展示状态码', () => {
    const response = { status: 502, data: '<html>Bad gateway</html>' } as AxiosResponse;
    expect(ragErrorMessage(new AxiosError('Request failed', 'ERR_BAD_RESPONSE', undefined, undefined, response))).toBe('请求失败（HTTP 502），后端未返回具体原因');
  });

  it('保留 Chat fetch 已解析的错误', () => {
    expect(ragErrorMessage(new Error('重建索引失败: 向量维度不匹配'))).toBe('重建索引失败: 向量维度不匹配');
  });
});
