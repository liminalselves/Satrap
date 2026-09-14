import { describe, expect, it } from 'vitest';
import type { ConversationItem } from '@/api/chat';
import { restoreConversation } from './conversations';

const item: ConversationItem = {
  conversation_id: 'old', title: '原会话', last_at: 1,
  turn_count: 1, model: 'model', think: 'off', project_id: 'project',
};

describe('会话恢复', () => {
  it('fork 刷新保留整个列表的项目归属', () => {
    const restored = [item, { ...item, conversation_id: 'fork' }]
      .map((entry) => restoreConversation(entry));
    expect(restored.map((entry) => entry.projectId)).toEqual(['project', 'project']);
    expect(restored.map((entry) => entry.loaded)).toEqual([false, false]);
  });

  it('合并服务器元数据时保留已经加载的消息', () => {
    const previous = { ...restoreConversation<string>(item), messages: ['消息'], loaded: true };
    const restored = restoreConversation({ ...item, title: '新标题', project_id: null }, previous);
    expect(restored.messages).toBe(previous.messages);
    expect(restored.loaded).toBe(true);
    expect(restored.title).toBe('新标题');
    expect(restored.projectId).toBeNull();
  });
});
