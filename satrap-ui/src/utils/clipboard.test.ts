import { afterEach, describe, expect, it, vi } from 'vitest';
import { copyTextToClipboard } from './clipboard';

describe('copyTextToClipboard', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('优先使用 Clipboard API 写入完整文本', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('navigator', { clipboard: { writeText } });

    await copyTextToClipboard('line 1\n  line 2');

    expect(writeText).toHaveBeenCalledOnce();
    expect(writeText).toHaveBeenCalledWith('line 1\n  line 2');
  });

  it('Clipboard API 失败时回退到 textarea', async () => {
    class FakeElement {
      value = '';
      style: Record<string, string> = {};
      setAttribute = vi.fn();
      focus = vi.fn();
      select = vi.fn();
      remove = vi.fn();
    }

    const textarea = new FakeElement();
    const activeElement = new FakeElement();
    const appendChild = vi.fn();
    const execCommand = vi.fn().mockReturnValue(true);
    vi.stubGlobal('navigator', {
      clipboard: { writeText: vi.fn().mockRejectedValue(new Error('denied')) },
    });
    vi.stubGlobal('document', {
      activeElement,
      createElement: vi.fn().mockReturnValue(textarea),
      body: { appendChild },
      execCommand,
    });

    await copyTextToClipboard('fallback text');

    expect(textarea.value).toBe('fallback text');
    expect(appendChild).toHaveBeenCalledWith(textarea);
    expect(execCommand).toHaveBeenCalledWith('copy');
    expect(textarea.remove).toHaveBeenCalledOnce();
    expect(activeElement.focus).toHaveBeenCalledOnce();
  });
});
