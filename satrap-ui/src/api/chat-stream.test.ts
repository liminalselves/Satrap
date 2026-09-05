import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { subscribeChat } from './chat';

vi.mock('@/utils/constants', () => ({ getChatApiUrl: () => 'http://localhost:19872' }));

class FakeSocket {
  static OPEN = 1;
  static CONNECTING = 0;
  static instances: FakeSocket[] = [];
  readyState = 1;
  onmessage?: (event: { data: string }) => void;
  onclose?: () => void;
  constructor(public url: string) { FakeSocket.instances.push(this); }
  receive(event: object) { this.onmessage?.({ data: JSON.stringify(event) }); }
  close() { this.readyState = 3; this.onclose?.(); }
}

const snapshot = (seq: number, stream_id = 'server-one') => ({
  type: 'snapshot', seq, stream_id, conversation_id: 'audit', state: 'idle',
  active_turn_id: null, turns: [], pending_user_inputs: [],
});

describe('聊天订阅重同步', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    FakeSocket.instances = [];
    vi.stubGlobal('WebSocket', FakeSocket);
  });
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

  it('发现序号断层后重连, 丢弃重复事件并接收权威快照', () => {
    const received = vi.fn();
    const stop = subscribeChat('audit', received);
    const first = FakeSocket.instances[0];
    first.receive(snapshot(4));
    first.receive({ type: 'content_delta', delta: 'a', seq: 5, stream_id: 'server-one' });
    first.receive({ type: 'content_delta', delta: 'a', seq: 5, stream_id: 'server-one' });
    first.receive({ type: 'turn_done', answer: 'ab', seq: 7, stream_id: 'server-one' });
    expect(received).toHaveBeenCalledTimes(2);
    vi.advanceTimersByTime(1000);
    expect(FakeSocket.instances).toHaveLength(2);
    FakeSocket.instances[1].receive({ ...snapshot(7), turns: [{ answer: 'ab' }] });
    expect(received.mock.lastCall?.[0].turns[0].answer).toBe('ab');
    stop();
  });

  it('服务重启和缓冲溢出均重新建立快照, 取消订阅后不重连', () => {
    const received = vi.fn();
    const stop = subscribeChat('audit', received);
    FakeSocket.instances[0].receive(snapshot(10));
    FakeSocket.instances[0].receive({ type: 'content_delta', delta: 'new', seq: 1, stream_id: 'server-two' });
    vi.advanceTimersByTime(1000);
    FakeSocket.instances[1].receive(snapshot(0, 'server-two'));
    FakeSocket.instances[1].receive({ type: 'resync_required' });
    stop();
    vi.advanceTimersByTime(60000);
    expect(FakeSocket.instances).toHaveLength(2);
    FakeSocket.instances[1].receive(snapshot(1));
    expect(received).toHaveBeenCalledTimes(2);
  });
});
