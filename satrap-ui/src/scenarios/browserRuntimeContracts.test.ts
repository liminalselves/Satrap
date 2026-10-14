import { afterEach, describe, expect, it, vi } from "vitest";
import { copyTextToClipboard } from "@/utils/clipboard";
import { getThinkingOptions, resolveRuntimeConfig } from "@/utils/constants";
import { formatBytes } from "@/utils/format";

describe("utils/clipboard", () => {
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
});

describe("utils/constants", () => {
  describe('getThinkingOptions', () => {
    it('filters configured levels for strength fields', () => {
      expect(getThinkingOptions({
        thinking_fields: ['thinking.type', 'reasoning_effort'],
        thinking_levels: ['low', 'xhigh', 'ultra'],
      })).toEqual([
        { value: 'off', label: '关闭' },
        { value: 'low', label: '低' },
        { value: 'xhigh', label: '高+' },
        { value: 'ultra', label: '最高' },
      ]);
    });

    it('uses the standard three levels for an existing config without metadata', () => {
      expect(getThinkingOptions({ thinking_fields: ['thinking_level'] })).toEqual([
        { value: 'off', label: '关闭' },
        { value: 'low', label: '低' },
        { value: 'medium', label: '中' },
        { value: 'high', label: '高' },
      ]);
    });

    it('shows one enabled state for switch-only models', () => {
      expect(getThinkingOptions({ thinking_fields: ['thinking.type'] })).toEqual([
        { value: 'off', label: '关闭' },
        { value: 'high', label: '开启' },
      ]);
    });

    it('keeps only off when the model has no thinking request field', () => {
      expect(getThinkingOptions()).toEqual([{ value: 'off', label: '关闭' }]);
    });
  });
});

describe("utils/runtimeConfig", () => {
  describe('resolveRuntimeConfig', () => {
    it('uses the default control endpoint when runtime discovery fails', () => {
      expect(resolveRuntimeConfig(null, 'http://127.0.0.1:5173')).toEqual({
        backend_api: 'http://127.0.0.1:19870',
        control_api: 'http://127.0.0.1:19871',
        chat_api: 'http://127.0.0.1:19872',
      });
    });

    it('uses discovered endpoints and the current page origin for control fallback', () => {
      expect(resolveRuntimeConfig({
        backend_api: 'http://127.0.0.1:29970',
        chat_api: 'http://127.0.0.1:29972',
      }, 'http://127.0.0.1:29971')).toEqual({
        backend_api: 'http://127.0.0.1:29970',
        control_api: 'http://127.0.0.1:29971',
        chat_api: 'http://127.0.0.1:29972',
      });
    });

    it('keeps build-time overrides as the highest priority', () => {
      expect(resolveRuntimeConfig({
        backend_api: 'http://discovered:1',
        control_api: 'http://discovered:2',
        chat_api: 'http://discovered:3',
      }, 'http://page', {
        backend: 'http://override:1',
        control: 'http://override:2',
        chat: 'http://override:3',
      })).toEqual({
        backend_api: 'http://override:1',
        control_api: 'http://override:2',
        chat_api: 'http://override:3',
      });
    });
  });
});

describe("utils/format", () => {
  it.each([
    [null, '大小未统计'], [0, '0 B'], [1023, '1023 B'],
    [1024, '1.0 KiB'], [1024 ** 2, '1.0 MiB'], [1024 ** 3, '1.0 GiB'],
  ] as const)('存储量 %s 显示为 %s', (value, expected) => {
    expect(formatBytes(value)).toBe(expected);
  });
});
