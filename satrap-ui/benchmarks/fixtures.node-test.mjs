import assert from 'node:assert/strict';
import test from 'node:test';
import { createBenchmarkFixture, jsonResponse, responseFor, installWebSocketMocks } from './fixtures.mjs';

test('凭据请求使用指定来源, 不使用通配来源', () => {
  const response = jsonResponse({}, 'http://127.0.0.1:4173');
  assert.equal(response.headers['Access-Control-Allow-Origin'], 'http://127.0.0.1:4173');
  assert.equal(response.headers['Access-Control-Allow-Credentials'], 'true');
  assert.match(response.headers['Access-Control-Allow-Headers'], /Authorization/);
});

test('未知接口不伪装成成功的空夹具', () => {
  assert.throws(() => responseFor({ url: () => 'http://127.0.0.1:19871/config/unknown', method: () => 'GET' }, createBenchmarkFixture()), /未匹配/);
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
