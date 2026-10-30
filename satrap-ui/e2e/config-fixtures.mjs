import assert from 'node:assert/strict';

// 未涉及模型配置的场景显式声明空配置, 避免通用成功回执冒充配置正文
export function emptyConfigResponse(requestUrl) {
  const url = new URL(requestUrl);
  if (url.pathname === '/config/models' || url.pathname === '/config/session-classes' || url.pathname === '/config/edictum/sessions') return {};
  if (url.pathname === '/config/plugins') return { plugins: [] };
  if (url.pathname === '/api/chat/settings') return { default_model: 'benchmark-model', revision: 'chat-default-fixture-v1' };
  if (url.pathname === '/api/chat/model') return { ok: true, model: 'benchmark-model', available: true };
  if (url.pathname === '/config/snapshot') {
    const resource = url.searchParams.get('resource');
    assert.ok(['models', 'session_classes', 'edictum_configs', 'plugin_catalog'].includes(resource), `未知快照资源: ${resource}`);
    const resources = resource === 'plugin_catalog' ? ['plugin_catalog', 'edictum_configs', 'chat_plugins']
      : resource === 'edictum_configs' ? ['edictum_configs', 'plugin_catalog'] : [resource];
    return { data: resource === 'plugin_catalog' ? [] : {}, versions: Object.fromEntries(resources.map((name) => [name, { state: 'available', change_token: 'empty-fixture-v1' }])) };
  }
  if (/^\/api\/chat\/conversations\/[^/]+\/settings$/.test(url.pathname)) return { temperature: null, request_temperature: 0.7 };
  assert.ok(!url.pathname.startsWith('/api/chat/conversations'), `未声明 Chat 目录夹具: ${url.pathname}`);
  return { ok: true };
}

// 目录夹具共享真实分页结构, 搜索和项目筛选均发生在切页之前
export function conversationPageResponse(requestUrl, conversations) {
  const query = new URL(requestUrl).searchParams;
  const id = query.get('conversation_id');
  let rows = conversations;
  if (id) rows = rows.filter((item) => item.conversation_id === id);
  const search = query.get('search')?.trim().toLowerCase();
  if (search) rows = rows.filter((item) => `${item.title} ${item.conversation_id}`.toLowerCase().includes(search));
  if (query.get('project_id')) rows = rows.filter((item) => item.project_id === query.get('project_id'));
  const offset = Number(query.get('cursor') || 0);
  const limit = Number(query.get('limit') || 50);
  assert.ok(Number.isInteger(offset) && offset >= 0 && Number.isInteger(limit) && limit > 0, '无效的分页夹具参数');
  const selected = rows.slice(offset, offset + limit);
  const more = !id && rows.length > offset + limit;
  return { conversations: selected, has_more: more, next_cursor: more ? String(offset + limit) : null };
}

// 历史按轮次向前分页, 首次返回最近一页并提供真实的下一页起点
export function turnPageResponse(requestUrl, turns) {
  const query = new URL(requestUrl).searchParams;
  const before = query.has('before') ? Number(query.get('before')) : Infinity;
  const limit = Number(query.get('limit') || 50);
  const rows = turns.filter((turn) => turn.turn_index < before).sort((a, b) => a.turn_index - b.turn_index);
  const selected = rows.slice(-limit);
  return { turns: selected, has_more: rows.length > limit, next_before: selected[0]?.turn_index ?? null };
}
