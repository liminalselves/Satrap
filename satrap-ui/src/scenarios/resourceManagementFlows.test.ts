import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AxiosAdapter, InternalAxiosRequestConfig } from 'axios';
import type { PlatformArchiveRecord } from '@/api/types';
import type { ScopedMemory, MemoryProposal } from '@/api/memory';
import type { GroupReminder } from '@/api/reminders';

vi.mock('@/utils/constants', () => ({ getApiBaseUrl: () => 'http://runtime', getControlApiUrl: () => 'http://control' }));
vi.mock('@/api/auth', () => ({ ensureApiSession: vi.fn().mockResolvedValue(undefined), establishApiSession: vi.fn() }));

const record = { platform_id: 'future/platform', self_id: 'bot:/a', conversation_kind: 'group', chat_id: '群/中文' } as PlatformArchiveRecord;
let requests: InternalAxiosRequestConfig[];
let respond: (config: InternalAxiosRequestConfig) => unknown;
let memoryApi: typeof import('@/api/memory').memoryApi;
let reminderApi: typeof import('@/api/reminders').reminderApi;
let backendApi: typeof import('@/api/backend').backendApi;
const body = (config: InternalAxiosRequestConfig) => config.data ? JSON.parse(config.data) : null;

beforeEach(async () => {
  vi.resetModules();
  requests = [];
  respond = () => { throw new Error('场景未声明的 HTTP 请求'); };
  const axios = (await import('axios')).default;
  const create = axios.create;
  const adapter: AxiosAdapter = async (config) => {
    requests.push(config);
    return { status: 200, statusText: 'OK', headers: {}, config, data: await respond(config) };
  };
  vi.spyOn(axios, 'create').mockImplementation((config) => {
    const client = create(config);
    client.defaults.adapter = adapter;
    return client;
  });
  ({ memoryApi } = await import('@/api/memory'));
  ({ reminderApi } = await import('@/api/reminders'));
  ({ backendApi } = await import('@/api/backend'));
});
afterEach(() => vi.restoreAllMocks());

describe('跨服务资源管理链路', () => {
  it('记忆查询, 创建修订零的提案审批和删除保持完整范围与操作标识', async () => {
    const memory = { memory_id: 'id/中文', revision: 3 } as ScopedMemory;
    respond = () => ({ ok: true, status: 'saved', items: [memory] });
    const page = await memoryApi.list(record, { kind: 'member_preference', user_id: 'member', keyword: '称呼' });
    expect(page.items).toEqual([memory]);
    await memoryApi.decide(record, { proposal_id: 'proposal', base_revision: 0 } as MemoryProposal, true);
    await memoryApi.remove(record, memory, 'same-intent');
    expect(requests.map((request) => [request.baseURL, request.method])).toEqual([
      ['http://control', 'get'], ['http://control', 'post'], ['http://control', 'delete'],
    ]);
    expect(requests[0]).toMatchObject({ url: '/api/platforms/future%2Fplatform/memory/memories', params: {
      self_id: 'bot:/a', conversation_kind: 'group', chat_id: '群/中文', kind: 'member_preference', user_id: 'member', keyword: '称呼',
    } });
    expect(requests[1].url).toBe('/api/platforms/future%2Fplatform/memory/proposals/proposal/decision');
    expect(body(requests[1])).toEqual({ approve: true, expected_revision: 0 });
    expect(requests[2].url).toBe('/api/platforms/future%2Fplatform/memory/memories/id%2F%E4%B8%AD%E6%96%87');
    expect(body(requests[2])).toEqual({ expected_revision: 3, idempotency_key: 'same-intent' });
    expect(requests.slice(1).map((request) => request.params)).toEqual([
      { self_id: 'bot:/a', chat_id: '群/中文', conversation_kind: 'group' },
      { self_id: 'bot:/a', chat_id: '群/中文', conversation_kind: 'group' },
    ]);
  });

  it('提醒从冷查询到运行时创建, 重试, 取消及恢复保持同一身份和修订', async () => {
    const reminder = { reminder_id: 'r/1', revision: 4 } as GroupReminder;
    respond = () => ({ ok: true, items: [reminder], reminder });
    expect((await reminderApi.list(record, { state: 'paused' })).items).toEqual([reminder]);
    const values = { text: '提醒', after_seconds: 30, mention_user_ids: ['member'], idempotency_key: 'same-intent' };
    await reminderApi.create(record, values);
    await reminderApi.create(record, values);
    await reminderApi.cancel(record, reminder);
    await reminderApi.resume(record, reminder);
    expect(requests.map((request) => request.baseURL)).toEqual(['http://control', 'http://runtime', 'http://runtime', 'http://control', 'http://runtime']);
    expect(requests[0].params).toEqual({ self_id: 'bot:/a', chat_id: '群/中文', conversation_kind: 'group', state: 'paused' });
    expect(requests[1].url).toBe(requests[2].url);
    for (const request of [requests[1], requests[2], requests[4]]) {
      const url = new URL(request.url!, request.baseURL);
      expect(url.pathname).toContain('/api/platforms/future%2Fplatform/group-chat/reminders');
      expect(Object.fromEntries(url.searchParams)).toEqual({ self_id: 'bot:/a', conversation_kind: 'group', chat_id: '群/中文' });
      expect(request.timeout).toBe(30000);
    }
    expect(body(requests[1])).toEqual(values);
    expect(body(requests[2])).toEqual(values);
    expect(requests[3].url).toBe('/api/platforms/future%2Fplatform/group-chat/reminders/r%2F1/cancel');
    expect(body(requests[3])).toEqual({ expected_revision: 4 });
    expect(new URL(requests[4].url!, requests[4].baseURL).pathname).toMatch(/\/r%2F1\/resume$/);
    expect(body(requests[4])).toEqual({ expected_revision: 4 });
  });

  it('平台探测保留成功和失败结果, 定向应用保留独立截止并传播超时', async () => {
    const success = { ok: true, detail: '通信正常, 对端已响应', elapsed_ms: 12 };
    const failure = { ok: false, detail: '通信请求超时 (8 秒)', elapsed_ms: 8000 };
    respond = () => success;
    expect(await backendApi.checkPlatformConnection('bot 中文')).toEqual(success);
    respond = () => failure;
    expect(await backendApi.checkPlatformConnection('mk')).toEqual(failure);
    expect(requests[0].url).toBe('/api/platforms/bot%20%E4%B8%AD%E6%96%87/connection-test');
    expect(body(requests[0])).toEqual({});
    respond = () => ({ ok: true, edictum_sessions: [] });
    const scope = { config_name: 'assistant' };
    const refs = { session_refs: [{ platform_id: 'ob', session_id: 'one' }] };
    await backendApi.reconcileEdictumPlugins(scope);
    await backendApi.reconcileEdictumRuntime(refs);
    expect(requests.slice(2).map((request) => [request.url, request.timeout, body(request)])).toEqual([
      ['/api/edictum/plugins/reconcile', 120000, scope], ['/api/edictum/runtime/apply', 120000, refs],
    ]);
    respond = () => { throw new Error('deadline exceeded'); };
    await expect(backendApi.reconcileEdictumPlugins(scope)).rejects.toThrow('deadline exceeded');
  });
});


describe('平台统一审批链路', () => {
  it('带完整身份查询并将三类决策交还原业务, 保留拒绝申请的原意', async () => {
    const { platformActionsApi, actionDescription, actionKey, approvalLink } = await import('@/api/platformActions');
    const item: import('@/api/platformActions').PlatformAction = {
      platform_id: 'bot/中文', self_id: '100', source: 'group', action_id: 'same', group_id: '888', action_type: 'ban_group_member',
      params: { user_id: '42', duration: 43200 }, actor_kind: 'model', actor_id: 'sender', state: 'pending', created_at: 1,
      expires_at: null, result: null, target: {}, can_decide: true, unavailable_reason: '',
    };
    respond = () => ({ state: 'succeeded' });
    await platformActionsApi.list({ platform: 'bot/中文', account: 'old', source: 'friend', action: 'same', state: 'all' });
    expect(requests[0].params).toMatchObject({ platform_id: 'bot/中文', account: 'old', source: 'friend', action_id: 'same', state: 'all', limit: 50 });
    await platformActionsApi.summary();
    expect(requests[1].url).toBe('/api/platform-actions/summary');
    await platformActionsApi.decide(item, true);
    const invitation = { ...item, source: 'invitation' as const, params: { approve: false }, action_type: 'handle_group_request' };
    await platformActionsApi.decide(invitation, true);
    await platformActionsApi.decide({ ...item, source: 'friend' }, false);
    expect(requests.slice(2).map((request) => [request.url, body(request)])).toEqual([
      ['/api/platforms/bot%2F%E4%B8%AD%E6%96%87/groups/888/actions/same/decision', { expected_self_id: '100', approve: true }],
      ['/api/platforms/bot%2F%E4%B8%AD%E6%96%87/account-requests/group-invites/actions/same/decision', { expected_self_id: '100', approve: true }],
      ['/api/platforms/bot%2F%E4%B8%AD%E6%96%87/friends/actions/same/decision', { expected_self_id: '100', approve: false }],
    ]);
    expect(actionDescription(invitation)).toContain('拒绝');
    expect(actionDescription(item)).toContain('12 小时');
    expect(actionKey(invitation)).not.toBe(actionKey(item));
    expect(approvalLink({ platform: 'bot/中文', account: '100', source: 'invitation', action: 'same' })).toContain('source=invitation');
    expect(approvalLink({ action: 'same' })).toContain('state=all');
    await platformActionsApi.list({ action: 'same' });
    expect(requests[requests.length - 1]?.params).toMatchObject({ state: 'all', action_id: 'same' });

    respond = () => { throw new Error('connection lost'); };
    const before = requests.length;
    await expect(platformActionsApi.decide(item, true)).rejects.toThrow('connection lost');
    expect(requests).toHaveLength(before + 1);
  });
});
