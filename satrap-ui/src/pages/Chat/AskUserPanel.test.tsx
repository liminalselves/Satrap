import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { AskUserPanel } from './AskUserPanel';

describe('AskUserPanel', () => {
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
