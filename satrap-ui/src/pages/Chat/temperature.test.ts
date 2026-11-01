import { describe, expect, it } from 'vitest';
import type { ChatGenerationSettings } from '@/api/chat';
import { TemperatureSync } from './temperature';

interface Deferred<T> { promise: Promise<T>; resolve: (value: T) => void; reject: (error: unknown) => void }

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

const drain = () => new Promise<void>((resolve) => setTimeout(resolve, 0));
const settings = (temperature: number | null): ChatGenerationSettings => ({ temperature, request_temperature: temperature });

describe('Chat 温度同步', () => {
  it('在途时只保留最新值, 旧响应不回写, 完成后继续发送最后值', async () => {
    const saves: Array<{ value: number | null; deferred: Deferred<ChatGenerationSettings> }> = [];
    const applied: Array<[string, ChatGenerationSettings]> = [];
    const sync = new TemperatureSync({
      save: (_id, value) => {
        const item = deferred<ChatGenerationSettings>();
        saves.push({ value, deferred: item });
        return item.promise;
      },
      onApplied: (id, result) => applied.push([id, result]),
    });
    sync.seed('conv', settings(0.2));

    sync.set('conv', 0.8);
    sync.set('conv', 0.9);
    expect(saves.map((item) => item.value)).toEqual([0.8]);

    saves[0].deferred.resolve(settings(0.8));
    await drain();
    expect(saves.map((item) => item.value)).toEqual([0.8, 0.9]);
    expect(applied).toEqual([]);

    saves[1].deferred.resolve(settings(0.9));
    await drain();
    expect(applied).toEqual([['conv', settings(0.9)]]);
    expect(sync.isBusy('conv')).toBe(false);
  });

  it('当前代次失败回退到最近确认值并报告错误', async () => {
    const saves: Array<Deferred<ChatGenerationSettings>> = [];
    const restored: Array<[string, ChatGenerationSettings | null]> = [];
    const errors: Array<[string, unknown]> = [];
    const sync = new TemperatureSync({
      save: () => {
        const item = deferred<ChatGenerationSettings>();
        saves.push(item);
        return item.promise;
      },
      onRestored: (id, confirmed) => restored.push([id, confirmed]),
      onError: (id, error) => errors.push([id, error]),
    });
    sync.seed('conv', settings(0.3));

    sync.set('conv', 0.9);
    saves[0].reject(new Error('network'));
    await drain();

    expect(restored).toEqual([['conv', settings(0.3)]]);
    expect(errors).toHaveLength(1);
    expect(String(errors[0][1])).toContain('network');
  });

  it('旧代次失败不覆盖最新待发送值, 最终值仍会落库', async () => {
    const saves: Array<Deferred<ChatGenerationSettings>> = [];
    const restored: Array<[string, ChatGenerationSettings | null]> = [];
    const applied: Array<[string, ChatGenerationSettings]> = [];
    const sync = new TemperatureSync({
      save: () => {
        const item = deferred<ChatGenerationSettings>();
        saves.push(item);
        return item.promise;
      },
      onRestored: (id, confirmed) => restored.push([id, confirmed]),
      onApplied: (id, result) => applied.push([id, result]),
    });
    sync.seed('conv', settings(0.1));

    sync.set('conv', 0.4);
    sync.set('conv', 0.7);
    saves[0].reject(new Error('old failure'));
    await drain();

    expect(restored).toEqual([]);
    expect(saves).toHaveLength(2);
    saves[1].resolve(settings(0.7));
    await drain();
    expect(applied).toEqual([['conv', settings(0.7)]]);
  });

  it('首次读取确认值前失败时回退到修改前的界面值', async () => {
    const saves: Array<Deferred<ChatGenerationSettings>> = [];
    const restored: Array<[string, ChatGenerationSettings | null]> = [];
    const sync = new TemperatureSync({
      save: () => {
        const item = deferred<ChatGenerationSettings>();
        saves.push(item);
        return item.promise;
      },
      onRestored: (id, confirmed) => restored.push([id, confirmed]),
    });

    sync.set('conv', 1.2, settings(0.4));
    saves[0].reject(new Error('network'));
    await drain();
    expect(restored).toEqual([['conv', settings(0.4)]]);
  });

  it('切换会话互不影响, 各会话独立串行', async () => {
    const saves: Array<{ id: string; value: number | null; deferred: Deferred<ChatGenerationSettings> }> = [];
    const applied: Array<[string, number | null]> = [];
    const sync = new TemperatureSync({
      save: (id, value) => {
        const item = deferred<ChatGenerationSettings>();
        saves.push({ id, value, deferred: item });
        return item.promise;
      },
      onApplied: (id, result) => applied.push([id, result.temperature]),
    });

    sync.set('a', 0.2);
    sync.set('b', 0.6);
    expect(saves.map((item) => [item.id, item.value])).toEqual([['a', 0.2], ['b', 0.6]]);

    saves[1].deferred.resolve(settings(0.6));
    saves[0].deferred.resolve(settings(0.2));
    await drain();
    expect(applied).toEqual([['b', 0.6], ['a', 0.2]]);
  });
});
