import { describe, expect, it } from 'vitest';
import { LogBuffer } from './logBuffer';

describe('有界日志缓冲', () => {
  it('突发输入只保留最新上限且顺序完整, 未更新的读取保留引用', () => {
    const buffer = new LogBuffer<number>(5000);
    for (let index = 0; index < 12000; index++) buffer.append(index);
    const snapshot = buffer.read();
    expect(snapshot.logs).toEqual(Array.from({ length: 5000 }, (_, index) => index + 7000));
    expect(buffer.read().logs).toBe(snapshot.logs);
    buffer.append(12000);
    expect(buffer.read().logs[0]).toBe(7001);
    expect(snapshot.logs[0]).toBe(7000);
  });

  it('暂停固定显示快照, 超过保留上限仍正确计数, 恢复显示最新完整缓冲', () => {
    const buffer = new LogBuffer<number>(3);
    [1, 2, 3].forEach((item) => buffer.append(item));
    buffer.pause(true);
    const frozen = buffer.read().logs;
    [4, 5, 6, 7].forEach((item) => buffer.append(item));
    expect(buffer.read()).toEqual({ logs: [1, 2, 3], bufferedCount: 4 });
    expect(buffer.read().logs).toBe(frozen);
    buffer.pause(false);
    expect(buffer.read()).toEqual({ logs: [5, 6, 7], bufferedCount: 0 });
  });

  it('暂停时清空包括未提交的缓冲, 后续输入不回放旧日志', () => {
    const buffer = new LogBuffer<string>(2);
    buffer.append('old'); buffer.pause(true); buffer.append('pending'); buffer.clear();
    buffer.append('new');
    expect(buffer.read()).toEqual({ logs: [], bufferedCount: 1 });
    buffer.pause(false);
    expect(buffer.read().logs).toEqual(['new']);
  });
});
