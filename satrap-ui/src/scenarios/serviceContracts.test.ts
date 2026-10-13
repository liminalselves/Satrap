import { describe, expect, it } from "vitest";
import { AxiosError, type AxiosResponse, type InternalAxiosRequestConfig } from "axios";
import { ApiError, toApiError } from "@/api/client";
import { parseEdictumConfigs, parseEdictumTypes, parseModelConfigs, parseSessionClassConfigs } from "@/api/control";
import { administratorError } from "@/api/administrators";
import { loggingError } from "@/api/logging";
import { ragErrorMessage } from "@/api/rag";
import { errorText } from "@/utils/errorText";

describe("api/client", () => {
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
});

describe("api/control", () => {
  describe('parseModelConfigs', () => {
    it('accepts a named model configuration map', () => {
      const configs = {
        default: {
          name: 'default',
          model: 'test-model',
        },
      };

      expect(parseModelConfigs(configs)).toEqual(configs);
    });

    it('rejects an HTML response instead of treating characters as configs', () => {
      expect(() => parseModelConfigs('<!DOCTYPE html>')).toThrow(TypeError);
    });

    it('rejects arrays instead of treating indexes as config names', () => {
      expect(() => parseModelConfigs([])).toThrow(TypeError);
    });
  });

  describe('parseSessionClassConfigs', () => {
    it('accepts a named session class configuration map', () => {
      const configs = {
        demo: {
          class_path: 'example.DemoSession',
          is_async: false,
          enabled: true,
        },
      };

      expect(parseSessionClassConfigs(configs)).toEqual(configs);
    });

    it('rejects an HTML response instead of treating characters as configs', () => {
      expect(() => parseSessionClassConfigs('<!DOCTYPE html>')).toThrow(TypeError);
    });
  });

  describe('Edictum response parsers', () => {
    it('accepts registered type metadata and named configs', () => {
      const types = {
        types: [{
          name: 'async_simple',
          is_async: true,
          description: 'test',
          config_schema: {},
          capabilities: { plugins: true, mcp: true, stream: true },
        }],
      };
      const configs = {
        assistant: {
          provider: 'edictum',
          edictum_type: 'async_simple',
          enabled: true,
          description: '',
          model_name: 'default',
          params: {},
          plugins: [],
        },
      };

      expect(parseEdictumTypes(types)).toEqual(types.types);
      expect(parseEdictumConfigs(configs)).toEqual(configs);
    });

    it('rejects malformed type and config payloads', () => {
      expect(() => parseEdictumTypes({ types: {} })).toThrow(TypeError);
      expect(() => parseEdictumConfigs([])).toThrow(TypeError);
      expect(() => parseEdictumConfigs({ invalid: 'value' })).toThrow(TypeError);
    });
  });
});

describe("api/errorMessages", () => {
  describe('公共错误格式与领域提示', () => {
    it.each([administratorError, loggingError, ragErrorMessage])('保留后端 detail 和 ApiError 的稳定错误码', (format) => {
      const error = new AxiosError('Request failed');
      error.response = { status: 409, data: { detail: '版本已变化' }, headers: {}, statusText: '', config: { headers: {} } } as typeof error.response;
      expect(format(error)).toBe('版本已变化');
      expect(format(new ApiError('请刷新', 409, 'conflict'))).toBe('请刷新 (conflict)');
    });

  });
});

describe("api/rag", () => {
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

    it('保留已解析的错误和非对象输入', () => {
      expect(ragErrorMessage(new Error('重建索引失败: 向量维度不匹配'))).toBe('重建索引失败: 向量维度不匹配');
      expect(ragErrorMessage(123)).toBe('123');
    });
  });
});

describe("utils/errorText", () => {
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
});
