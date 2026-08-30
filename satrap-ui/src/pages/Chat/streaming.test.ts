import { describe, expect, it } from 'vitest';
import { applyStreamingDeltas, enqueueStreamingDelta, type StreamingDelta } from './streaming';

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
