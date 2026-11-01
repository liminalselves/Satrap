import { describe, expect, it } from 'vitest';
import {
  attachmentCapabilityBlocked,
  bindingFailureProblem,
  bindingProblemFromAuthoritative,
  modelBindingProblem,
  resolveModelReadiness,
  type ModelBindingProblem,
} from './readiness';

const readyInput = {
  isDraft: false,
  hasDefaults: true,
  model: 'fast',
  modelsLoaded: true,
  models: ['fast', 'deep'],
  bindingPending: false,
  bindingProblem: null as ModelBindingProblem | null,
};

describe('Chat 模型就绪状态', () => {
  it('已有会话不依赖整体默认模型, 草稿仍要求默认模型', () => {
    expect(resolveModelReadiness({ ...readyInput, hasDefaults: false })).toMatchObject({ ready: true, reason: null });
    const draft = resolveModelReadiness({ ...readyInput, isDraft: true, hasDefaults: false });
    expect(draft.ready).toBe(false);
    expect(draft.reason).toBe('defaults');
  });

  it('区分暂时绑定失败与确认模型不可用', () => {
    const temporary = resolveModelReadiness({
      ...readyInput,
      bindingProblem: { kind: 'temporary', message: '读取失败' },
    });
    expect(temporary).toMatchObject({ ready: false, reason: 'binding' });

    const unavailable = resolveModelReadiness({
      ...readyInput,
      bindingProblem: { kind: 'unavailable', message: '模型不存在' },
    });
    expect(unavailable).toMatchObject({ ready: false, reason: 'unavailable' });

    const deleted = resolveModelReadiness({ ...readyInput, models: ['deep'] });
    expect(deleted.ready).toBe(false);
    expect(deleted.reason).toBe('unavailable');
    expect(deleted.problem?.message).toContain('fast');
  });

  it('模型目录未知时不猜测不可用, 绑定确认中阻止发送', () => {
    expect(resolveModelReadiness({ ...readyInput, modelsLoaded: false, models: [] }).ready).toBe(true);
    expect(resolveModelReadiness({ ...readyInput, bindingPending: true })).toMatchObject({ ready: false, reason: 'busy' });
  });

  it('getModel 结果映射为可重试或确认不可用', () => {
    expect(modelBindingProblem({ ok: false, model: '', available: false, error: '超时' })).toEqual({ kind: 'temporary', message: '超时' });
    expect(modelBindingProblem({ ok: true, model: 'fast', available: false })).toMatchObject({ kind: 'unavailable' });
    expect(modelBindingProblem({ ok: true, model: 'fast', available: true })).toBeNull();
    expect(bindingFailureProblem(new Error('断线'))).toMatchObject({ kind: 'temporary' });
  });

  it('权威 model_changed 与 snapshot 清除暂时问题, 无模型的 snapshot 不清除', () => {
    const current: ModelBindingProblem = { kind: 'temporary', message: '读取失败' };
    expect(bindingProblemFromAuthoritative('fast', true, current)).toBeNull();
    expect(bindingProblemFromAuthoritative('fast', undefined, current)).toBeNull();
    expect(bindingProblemFromAuthoritative(undefined, undefined, current)).toBe(current);
    expect(bindingProblemFromAuthoritative('fast', false, current)).toMatchObject({ kind: 'unavailable' });
  });

  it('媒体能力未知不阻止发送, 明确不支持才拦截对应媒体', () => {
    expect(attachmentCapabilityBlocked('image/png', undefined)).toBe(false);
    expect(attachmentCapabilityBlocked('image/png', {})).toBe(false);
    expect(attachmentCapabilityBlocked('image/png', { supports_visual_input: true })).toBe(false);
    expect(attachmentCapabilityBlocked('image/png', { supports_visual_input: false })).toBe(true);
    expect(attachmentCapabilityBlocked('video/mp4', { supports_visual_input: false })).toBe(false);
    expect(attachmentCapabilityBlocked('video/mp4', { supports_visual_input: false, supports_native_video_input: false })).toBe(true);
    expect(attachmentCapabilityBlocked('video/mp4', { supports_visual_input: true, supports_native_video_input: false })).toBe(false);
    expect(attachmentCapabilityBlocked('text/plain', { supports_visual_input: false })).toBe(false);
  });
});
