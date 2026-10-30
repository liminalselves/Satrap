import { describe, expect, it } from "vitest";
import { AxiosError, type AxiosResponse, type InternalAxiosRequestConfig } from "axios";
import { ApiError, toApiError } from "@/api/client";
import { parseEdictumConfigs, parseEdictumTypes, parseModelConfigs, parseSessionClassConfigs } from "@/api/control";
import { administratorError } from "@/api/administrators";
import { loggingError } from "@/api/logging";
import { ragErrorMessage } from "@/api/rag";
import { errorText } from "@/utils/errorText";
import { debugMode, withDebugMode, sameDebugObject, type DebugPolicy } from '@/api/debugLogging';
import { diagnosticTone, eventPresentation, inputDeltaPresentation, requestHint } from '@/pages/Logs/debugPresentation';

describe('高级诊断对象设置', () => {
  it('覆盖绑定完整对象, 切换上下文不复制覆盖, reset 恢复默认且不修改原草稿', () => {
    const group = { platform_id: 'qq', self_id: 'bot', conversation_kind: 'group' as const, target_id: 'group' };
    const policy: DebugPolicy = { version: 1, default_enabled: false, retention_days: 7, max_total_mb: 512, max_body_mb: 8, max_request_mb: 32, objects: [] };
    const enabled = withDebugMode(policy, group, 'on');
    expect(policy.objects).toEqual([]);
    expect(debugMode(enabled, { ...group })).toBe('on');
    for (const changed of [{ ...group, self_id: 'other' }, { ...group, platform_id: 'other' }, { ...group, target_id: 'other' }, { ...group, conversation_kind: 'private' as const }]) {
      expect(sameDebugObject(group, changed)).toBe(false);
      expect(debugMode(enabled, changed)).toBe('inherit');
    }
    expect(withDebugMode(enabled, group, 'inherit').objects).toEqual([]);
    expect(debugMode(withDebugMode(enabled, group, 'off'), group)).toBe('off');
  });
});

describe('高级诊断标题与级别', () => {
  it('输入增量说明隐藏历史和移除范围, 不把压缩后的改变伪装成只追加', () => {
    const delta = { view: 'delta', basis: 'previous_model_input', hidden_prefix_messages: 10, hidden_suffix_messages: 2,
      removed_messages: 3, messages: [{ role: 'system', content: '新摘要' }], changed_parameters: {}, removed_parameters: ['temperature'] };
    const presentation = inputDeltaPresentation(delta);
    expect(presentation.summary).toContain('移除 3 条');
    expect(presentation.summary).toContain('隐藏未变消息 12 条');
    expect(presentation.text).toContain('新摘要');
    expect(presentation.text).toContain('temperature');
    expect(inputDeltaPresentation({ ...delta, basis: 'latest_user_message', baseline_unavailable: true }).summary).toContain('基线不可用');
    expect(inputDeltaPresentation({ original: '旧正文' }).summary).toBeUndefined();
  });
  it('业务终态与异常颜色分别保留, 旧记录不猜测, 标题不读取动态正文', () => {
    const request = { request_id: 'request', context_id: 'context', parent_request_id: '', status: 'success', incomplete: 0,
      updated_at: 2, started_at: 1, settings_revision: 'v1' };
    expect(requestHint({ ...request, diagnostic_level: 'warning' })).toBe('含调用问题');
    expect(requestHint({ ...request, diagnostic_level: 'error' })).toBe('含系统错误');
    expect(requestHint(request)).toBe('旧记录未分类');
    expect(diagnosticTone('info', 'cancelled')).toBe('neutral');
    expect(diagnosticTone('error', 'cancelled')).toBe('error');
    expect(diagnosticTone('info', 'success', true)).toBe('warning');
    const event = { event_id: 'event', sequence: 1, category: 'tool_result', phase: 'end', span_id: '', parent_span_id: '',
      status: 'returned', recorded_at: 1, body_slots: ['result'], metadata: {} };
    expect(eventPresentation(event).tone).toBe('unknown');
    const warning = eventPresentation({ ...event, metadata: { diagnostic_level: 'warning', name: 'read_document', error_kind: 'model_input', error_code: 'invalid_arguments' } });
    expect(warning.title).toBe('工具返回 · read_document · 参数或调用错误');
    expect(warning.tone).toBe('warning');
    expect(eventPresentation({ ...event, metadata: { diagnostic_level: 'info', result_status: 'pending' } }).tone).toBe('neutral');
    expect(eventPresentation({ ...event, metadata: { diagnostic_level: 'info', result_status: 'sent' } }).title).toBe('工具返回 · 已发送');
    expect(eventPresentation({ ...event, metadata: { diagnostic_level: 'error', result_status: 'partial' } }).title).toBe('工具返回 · 部分完成 · 系统错误');
    expect(eventPresentation({ ...event, metadata: { diagnostic_level: 'warning', result_status: 'unknown', error_kind: 'uncertain_result' } }).title).toBe('工具返回 · 结果待确认');
    expect(eventPresentation({ ...event, metadata: { diagnostic_level: 'info', data: { ok: false, error_type: 'execution_error' } } }).tone).toBe('normal');
  });
});

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
