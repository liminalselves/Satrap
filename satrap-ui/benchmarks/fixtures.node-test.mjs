import assert from 'node:assert/strict';
import test from 'node:test';
import { emptyConfigResponse } from '../e2e/config-fixtures.mjs';
import { createBenchmarkFixture, jsonResponse, responseFor, installWebSocketMocks } from './fixtures.mjs';

test('凭据请求使用指定来源, 不使用通配来源', () => {
  const response = jsonResponse({}, 'http://127.0.0.1:4173');
  assert.equal(response.headers['Access-Control-Allow-Origin'], 'http://127.0.0.1:4173');
  assert.equal(response.headers['Access-Control-Allow-Credentials'], 'true');
  assert.match(response.headers['Access-Control-Allow-Headers'], /Authorization/);
});

test('未知接口不伪装成成功的空夹具', () => {
  assert.throws(() => responseFor({ url: () => 'http://127.0.0.1:19871/config/unknown', method: () => 'GET' }, createBenchmarkFixture()), /未匹配/);
  assert.throws(() => emptyConfigResponse('http://127.0.0.1:19872/api/chat/conversations/page'), /未声明 Chat 目录夹具/);
});

test('状态消息符合客户端 data 信封', async () => {
  const messages = [];
  await installWebSocketMocks({ routeWebSocket: async (url, handler) => {
    handler({ send: (value) => messages.push(JSON.parse(value)), onClose: () => {} });
  } }, new Set());
  const status = messages.find((item) => item.type === 'status');
  assert.equal(status.data.running, true);
  assert.ok(status.data.adapters['benchmark-adapter']);
  assert.equal(status.running, undefined);
});


test('目录分页和仪表盘概览使用当前生产契约', () => {
  const fixture = createBenchmarkFixture(100);
  const request = (path) => ({ url: () => 'http://127.0.0.1:19872' + path, method: () => 'GET' });
  const page = responseFor(request('/api/chat/conversations/page?limit=50'), fixture);
  assert.equal(page.conversations.length, 50);
  assert.equal(page.has_more, true);
  const next = responseFor(request('/api/chat/conversations/page?limit=50&cursor=' + page.next_cursor), fixture);
  assert.equal(next.conversations.length, 10);
  assert.equal(next.has_more, false);
  const exact = responseFor(request('/api/chat/conversations/page?conversation_id=benchmark-59'), fixture);
  assert.deepEqual(exact.conversations.map((item) => item.conversation_id), ['benchmark-59']);
  const search = responseFor(request('/api/chat/conversations/page?search=' + encodeURIComponent('性能测试会话 59')), fixture);
  assert.equal(search.conversations[0].conversation_id, 'benchmark-59');
  const history = createBenchmarkFixture(120);
  const recent = responseFor(request('/api/chat/turns/page?limit=50'), history);
  assert.equal(recent.turns.length, 50);
  assert.equal(recent.has_more, true);
  const older = responseFor(request('/api/chat/turns/page?limit=50&before=' + recent.next_before), history);
  assert.equal(older.turns.length, 10);
  assert.equal(older.has_more, false);
  assert.equal(older.turns.at(-1).turn_index + 1, recent.turns[0].turn_index);
  const overview = responseFor(request('/config/overview'), fixture);
  assert.equal(overview.model_counts.llm, 1);
  assert.equal(typeof overview.session_count, 'number');
});
