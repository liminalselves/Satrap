import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { AskUserPanel } from "@/pages/Chat/AskUserPanel";
import { OutputFiles } from "@/pages/Chat/OutputFiles";
import { type ConversationItem } from "@/api/chat";
import { conversationBoundaryReached, restoreConversation, sidebarConversationVisible, type Conversation } from "@/pages/Chat/conversations";
import { applyStreamingDeltas, enqueueStreamingDelta, type StreamingDelta } from "@/pages/Chat/streaming";
import { REJECTION_STAGE_FILTER, RequestDiagnosticsPanel, attachmentLabel, reasonLabel } from "@/components/diagnostics/RequestDiagnosticsPanel";

describe('Chat 文件交付', () => {
  it('显示下载按钮、名称和大小, HTML 文件不以内联内容渲染', () => {
    const markup = renderToStaticMarkup(<OutputFiles conversationId="chat" files={[{
      file_id: 'sf_123', name: '报告<script>.html', size: 1024, sha256: 'hash', mime: 'text/html',
    }]} />);
    expect(markup).toContain('下载 报告&lt;script&gt;.html');
    expect(markup).toContain('1.0 KiB');
    expect(markup).not.toContain('<iframe');
    expect(markup).not.toContain('<script>');
  });
});

describe("pages/Chat/AskUserPanel", () => {
  describe('AskUserPanel', () => {
    it('审批只展示本次动作的折叠卡片并提供明确决定按钮', () => {
      const html = renderToStaticMarkup(
        <AskUserPanel request={{ requestId: 'approval-1', question: '是否批准本次编程操作?', options: ['批准本次', '拒绝'],
          approval: { tool: 'shell', operation: 'shell', description: '执行本地命令',
            action: { command: 'Get-ChildItem', cwd: 'D:/project', interpreter: 'powershell.exe' } } }}
          answer="" queueLength={1} submitting={false} onAnswerChange={vi.fn()} onSubmit={vi.fn()} />,
      );
      expect(html).toContain('等待批准执行 · shell');
      expect(html).toContain('Get-ChildItem');
      expect(html).toContain('D:/project');
      expect(html).toContain('<details');
      expect(html).not.toMatch(/<details[^>]*\bopen/);
      expect(html).not.toContain('<textarea');
      expect(html).not.toContain('跳过');
      expect(html).not.toContain('输入自定义回答');
      expect(html).toContain('批准本次');
      expect(html).toContain('拒绝');
    });

    it('渲染结构化选项和当前选中状态', () => {
      const html = renderToStaticMarkup(
        <AskUserPanel
          request={{
            requestId: 'request-1',
            question: '下一步做什么?',
            options: ['提交改动', '继续优化'],
          }}
          answer="继续优化"
          queueLength={2}
          submitting={false}
          onAnswerChange={vi.fn()}
          onSubmit={vi.fn()}
        />,
      );

      expect(html).toContain('下一步做什么?');
      expect(html).toContain('提交改动');
      expect(html).toContain('继续优化');
      expect(html).toContain('aria-selected="true"');
      expect(html).toContain('1 / 2');
    });

    it('没有选项时仍提供自定义回答入口', () => {
      const html = renderToStaticMarkup(
        <AskUserPanel
          request={{ requestId: 'request-2', question: '请说明原因', options: [] }}
          answer=""
          queueLength={1}
          submitting={false}
          onAnswerChange={vi.fn()}
          onSubmit={vi.fn()}
        />,
      );

      expect(html).not.toContain('role="listbox"');
      expect(html).toContain('输入自定义回答...');
    });
  });
});

describe("pages/Chat/conversations", () => {
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

    it('迟到的目录保留已确认模型, 初次发现会话才从目录初始化绑定', () => {
      const previous = { ...restoreConversation(item), model: 'manually-selected' };
      expect(restoreConversation({ ...item, model: 'old-model' }, previous).model).toBe('manually-selected');
      expect(restoreConversation({ ...item, model: 'initial-model' }).model).toBe('initial-model');
    });
  });
});

describe("pages/Chat/streaming", () => {
  describe('Chat streaming batching', () => {
    it('在队列中合并连续同类增量并保留交错顺序', () => {
      const queue: StreamingDelta[] = [];
      enqueueStreamingDelta(queue, { type: 'thinking', delta: '先' });
      enqueueStreamingDelta(queue, { type: 'thinking', delta: '想' });
      enqueueStreamingDelta(queue, { type: 'content', delta: '答' });
      enqueueStreamingDelta(queue, { type: 'content', delta: '案' });
      enqueueStreamingDelta(queue, { type: 'thinking', delta: '补充' });
      expect(queue).toEqual([
        { type: 'thinking', delta: '先想' },
        { type: 'content', delta: '答案' },
        { type: 'thinking', delta: '补充' },
      ]);
    });

    it('单次应用一帧内全部增量并保持分段语义', () => {
      const result = applyStreamingDeltas(
        {
          content: '已有',
          thinking: '旧思考',
          segments: [{ type: 'content' as const, content: '已有' }],
        },
        [
          { type: 'content', delta: '内容' },
          { type: 'thinking', delta: '新思考' },
          { type: 'content', delta: '结论' },
        ],
      );
      expect(result.content).toBe('已有内容结论');
      expect(result.thinking).toBe('旧思考新思考');
      expect(result.segments).toEqual([
        { type: 'content', content: '已有内容' },
        { type: 'thinking', content: '新思考' },
        { type: 'content', content: '结论' },
      ]);
    });

    it('只有内容增量时不创建空的思考字段', () => {
      const result = applyStreamingDeltas(
        { content: '', segments: [] },
        [{ type: 'content', delta: '完成' }],
      );
      expect(result.content).toBe('完成');
      expect(result).not.toHaveProperty('thinking');
    });

    it('大量连续增量在提交前压缩为一个队列项', () => {
      const queue: StreamingDelta[] = [];
      for (let index = 0; index < 1000; index += 1) {
        enqueueStreamingDelta(queue, { type: 'content', delta: 'x' });
      }
      expect(queue).toHaveLength(1);
      expect(queue[0].delta).toHaveLength(1000);
    });
  });
});

describe("components/diagnostics/RequestDiagnosticsPanel", () => {
  // 静态渲染只验证控件契约 (勾选/禁用/入口), 取数与筛选的实际行为由 Playwright 覆盖
  function rejectionCheckbox(markup: string): string {
    const match = /<input[^>]*data-testid="diagnostics-rejections-only"[^>]*>/.exec(markup);
    if (!match) throw new Error('未找到"仅看拒绝"开关');
    return match[0];
  }

  describe('RequestDiagnosticsPanel', () => {
    it('仅看拒绝预设默认勾选, 平台页默认不勾选', () => {
      const preset = renderToStaticMarkup(<RequestDiagnosticsPanel rejectionsOnlyDefault />);
      expect(preset).toContain('仅看拒绝');
      expect(rejectionCheckbox(preset)).toContain('checked=""');
      const platformPage = renderToStaticMarkup(<RequestDiagnosticsPanel />);
      expect(rejectionCheckbox(platformPage)).not.toContain('checked=""');
    });

    it('聚焦请求后取消并禁用拒绝筛选, 提供返回近期请求入口', () => {
      const focused = renderToStaticMarkup(
        <RequestDiagnosticsPanel requestId="req-1" rejectionsOnlyDefault onClearRequest={vi.fn()} />,
      );
      expect(rejectionCheckbox(focused)).toContain('disabled=""');
      expect(rejectionCheckbox(focused)).not.toContain('checked=""');
      expect(focused).toContain('data-testid="diagnostics-clear-request"');
    });

    it('未提供清除入口时不渲染返回按钮 (平台页无聚焦请求)', () => {
      const focused = renderToStaticMarkup(<RequestDiagnosticsPanel requestId="req-1" />);
      expect(focused).not.toContain('diagnostics-clear-request');
    });

    it('拒绝预设的阶段取值与后端拒绝阶段一致', () => {
      expect(REJECTION_STAGE_FILTER).toBe('wake_decision,rate_limit');
    });

    it('图片与视频媒体诊断码有中文标签', () => {
      expect(attachmentLabel('image:failed:image_url_refreshed')).toBe('图片 失败 (图片地址已刷新)');
      expect(attachmentLabel('video:failed:video_unsupported_by_implementation')).toBe('视频 失败 (当前实现不支持视频回源)');
      expect(reasonLabel('image_unavailable')).toBe('图片无法获取');
      expect(reasonLabel('image_media_budget_dropped')).toBe('图片超出本事件媒体预算');
    });
  });
});

describe('本次打开会话的侧栏生命周期', () => {
  it('首条发送和冷历史打开后切走仍可见, 目录刷新保留本地标记', () => {
    const listed = new Set(['other']);
    const sent: Conversation<never> = { id: 'sent', title: '新对话', messages: [], updatedAt: 1, preloaded: false, locallyOpened: true };
    const cold = { ...sent, id: 'cold', title: '旧历史' };
    for (const item of [sent, cold]) {
      expect(sidebarConversationVisible(item, listed, 'other', '', '')).toBe(true);
      expect(sidebarConversationVisible(item, listed, 'other', '不匹配', '')).toBe(false);
      expect(sidebarConversationVisible(item, listed, 'other', '', 'different')).toBe(false);
    }
    const restored = restoreConversation({ conversation_id: 'sent', title: '新对话', last_at: 2, turn_count: 0 }, sent);
    expect(restored.locallyOpened).toBe(true);
  });
  it('搜索中创建的当前分支可见, 普通缓存不会混进服务端目录', () => {
    const item: Conversation<never> = { id: 'fork', title: '分支', messages: [], updatedAt: 1 };
    expect(sidebarConversationVisible(item, new Set(), 'fork', '原会话', '')).toBe(true);
    expect(sidebarConversationVisible(item, new Set(), 'other', '', '')).toBe(false);
  });
});


describe('项目变动后的侧栏筛选', () => {
  it('刷新覆盖原排序边界, 边界删除和非 ASCII ID 的顺序与服务端一致', () => {
    const item = (id: string, time = 10): ConversationItem => ({ conversation_id: id, last_at: time, title: id, turn_count: 0 });
    expect(conversationBoundaryReached(item('a', 11), item('b'))).toBe(false);
    expect(conversationBoundaryReached(item('a', 9), item('b'))).toBe(true);
    expect(conversationBoundaryReached(item('a'), item('b'))).toBe(false);
    expect(conversationBoundaryReached(item('b'), item('b'))).toBe(true);
    expect(conversationBoundaryReached(item('c'), item('b'))).toBe(true);
    expect(conversationBoundaryReached(item('😀'), item('\uffff'))).toBe(true);
    expect(conversationBoundaryReached(item('\uffff'), item('😀'))).toBe(false);
    expect(conversationBoundaryReached(undefined, null)).toBe(true);
  });
  it('目录中仍有旧 ID 时也遵守最新项目归属, 保留服务端正文搜索结果', () => {
    const item: Conversation<never> = { id: 'moved', title: '标题', projectId: 'b', messages: [], updatedAt: 1 };
    const listed = new Set(['moved']);
    expect(sidebarConversationVisible(item, listed, 'moved', '', 'a')).toBe(false);
    expect(sidebarConversationVisible(item, listed, 'other', '正文匹配', 'b')).toBe(true);
    expect(sidebarConversationVisible({ ...item, projectId: null }, listed, 'moved', '', 'b')).toBe(false);
  });
});
