const FIXED_NOW = 1_788_100_000;

function createTurn(index) {
  const answer = index % 10 === 0
    ? `第 ${index + 1} 轮回答\n\n\`\`\`python\nvalue = ${index}\nprint(value)\n\`\`\``
    : `第 ${index + 1} 轮回答, 用于测量长历史消息的布局和滚动性能`;
  return {
    id: index + 1,
    turn_index: index,
    user_input: `第 ${index + 1} 轮用户消息, 请解释当前测试内容`,
    thinking: index % 5 === 0 ? `第 ${index + 1} 轮思考内容` : null,
    answer,
    attachments: null,
    segments: [{ type: 'content', content: answer }],
    created_at: FIXED_NOW - index * 60,
    tool_calls: [],
    active_variant: 0,
    variant_count: 1,
    variants: [],
  };
}

export function createBenchmarkFixture(messageCount = 100) {
  const turnCount = Math.ceil(messageCount / 2);
  const conversations = Array.from({ length: 60 }, (_, index) => ({
    conversation_id: index === 0 ? 'benchmark-active' : `benchmark-${index}`,
    turn_count: index === 0 ? turnCount : index % 8,
    last_at: FIXED_NOW - index * 120,
    title: index === 0 ? `Benchmark ${messageCount} 条消息` : `性能测试会话 ${index}`,
    project_id: null,
    model: 'benchmark-model',
    think: 'off',
    created_at: FIXED_NOW - index * 600,
  }));
  return {
    messageCount,
    errors: [],
    conversations,
    turns: Array.from({ length: turnCount }, (_, index) => createTurn(index)),
  };
}

export function jsonResponse(data, origin) {
  return {
    status: 200,
    contentType: 'application/json; charset=utf-8',
    headers: {
      'Access-Control-Allow-Origin': origin,
      'Access-Control-Allow-Credentials': 'true',
      'Access-Control-Allow-Headers': 'Content-Type, Authorization',
      'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS',
      Vary: 'Origin',
    },
    body: JSON.stringify(data),
  };
}

export function responseFor(request, fixture) {
  const url = new URL(request.url());
  const path = url.pathname;
  const method = request.method();

  if (path === '/auth/session' && method === 'POST') return { ok: true };

  if (path === '/api/chat/health') return { ok: true, conversations: fixture.conversations.length, preloaded: 0 };
  if (path === '/api/chat/models/detail') {
    return {
      ok: true,
      models: {
        'benchmark-model': {
          name: 'benchmark-model',
          model: 'benchmark-model',
          thinking_fields: ['reasoning_effort'],
          thinking_levels: ['low', 'medium', 'high'],
        },
      },
    };
  }
  if (path === '/api/chat/models') return { models: ['benchmark-model'] };
  if (path === '/api/chat/plugins') return { plugins: [] };
  if (path === '/api/chat/runs') return { runs: [] };
  if (path === '/api/chat/conversations' && method === 'GET') return { conversations: fixture.conversations };
  if (path === '/api/chat/conversations/preload') return { ok: true, conversation_id: 'benchmark-preloaded' };
  if (path === '/api/chat/turns') return { turns: fixture.turns };
  if (path === '/api/chat/send') {
    return {
      ok: true,
      conversation_id: 'benchmark-active',
      turn_id: fixture.turns.length + 1,
      turn_index: fixture.turns.length,
      variant_index: 0,
    };
  }
  if (path === '/api/projects') return { projects: [] };
  if (path === '/api/health') {
    return {
      running: true,
      adapters: {
        'benchmark-adapter': { status: 'running', started: true, type: 'benchmark' },
      },
    };
  }
  if (path === '/status') return { running: true, managed: true };
  if (path === '/config/models/options') return { llm: ['benchmark-model'], embedding: [], rerank: [], asr: [] };
  if (path === '/config/models' && method === 'GET') return {};
  if (/^\/config\/models\/(llm|embedding|rerank|asr)$/.test(path)) return {};
  if (path === '/config/session-classes' || path === '/config/edictum/sessions') return {};
  throw new Error(`未匹配性能夹具: ${method} ${path}`);
}

/** 为页面安装与后端隔离的固定 HTTP 夹具 */
export async function installHttpMocks(context, appOrigin, fixture) {
  await context.route(`${appOrigin}/ui-config.json`, async (route) => {
    await route.fulfill(jsonResponse({
      backend_api: 'http://127.0.0.1:19870',
      control_api: 'http://127.0.0.1:19871',
      chat_api: 'http://127.0.0.1:19872',
    }, appOrigin));
  });

  const handler = async (route) => {
    const request = route.request();
    if (request.method() === 'OPTIONS') {
      await route.fulfill(jsonResponse({}, appOrigin));
      return;
    }
    try {
      await route.fulfill(jsonResponse(responseFor(request, fixture), appOrigin));
    } catch (error) {
      fixture.errors.push(String(error));
      await route.fulfill({ ...jsonResponse({ error: String(error) }, appOrigin), status: 501 });
    }
  };
  await context.route('http://127.0.0.1:19870/**', handler);
  await context.route('http://127.0.0.1:19871/**', handler);
  await context.route('http://127.0.0.1:19872/**', handler);
}

/** 为页面安装可由测试脚本驱动的固定 WebSocket 夹具 */
export async function installWebSocketMocks(page, chatSockets) {
  await page.routeWebSocket('ws://127.0.0.1:19872/ws/chat**', (socket) => {
    chatSockets.add(socket);
    socket.onClose(() => chatSockets.delete(socket));
    socket.send(JSON.stringify({ type: 'subscribed', conversation_id: 'benchmark-active' }));
  });
  await page.routeWebSocket('ws://127.0.0.1:19870/ws/status**', (socket) => {
    socket.send(JSON.stringify({
      type: 'status',
      data: {
        running: true,
        adapters: {
          'benchmark-adapter': { status: 'running', started: true, type: 'benchmark' },
        },
      },
    }));
  });
}
