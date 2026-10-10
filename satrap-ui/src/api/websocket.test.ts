import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SatrapWebSocket } from './websocket';

vi.mock('@/utils/constants', () => ({ getApiBaseUrl: () => 'http://localhost:19870' }));

class TestSocket {
  static OPEN = 1;
  static instances: TestSocket[] = [];
  readyState = 0;
  onclose: ((event: { code: number; reason: string }) => void) | null = null;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  constructor(public url: string) { TestSocket.instances.push(this); }
  close(code = 1006, reason = '') { this.readyState = 3; this.onclose?.({ code, reason }); }
}

beforeEach(() => {
  vi.useFakeTimers();
  TestSocket.instances = [];
  vi.stubGlobal('WebSocket', TestSocket);
  vi.spyOn(console, 'log').mockImplementation(() => undefined);
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe('状态订阅生命周期', () => {
  it('意外断开后重连并继续传递状态', () => {
    const onStatus = vi.fn();
    const socket = new SatrapWebSocket('/ws/status');
    socket.connect({ onStatus });
    TestSocket.instances[0].close();
    vi.advanceTimersByTime(1000);
    expect(TestSocket.instances).toHaveLength(2);
    TestSocket.instances[1].onmessage?.({ data: JSON.stringify({ type: 'status', data: { running: true, adapters: {} } }) });
    expect(onStatus).toHaveBeenCalledWith({ running: true, adapters: {} });
    socket.disconnect();
  });

  it('卸载时取消已排队的重连, 不遗留新的订阅', () => {
    const socket = new SatrapWebSocket('/ws/status');
    socket.connect({});
    TestSocket.instances[0].close();
    socket.disconnect();
    vi.advanceTimersByTime(60000);
    expect(TestSocket.instances).toHaveLength(1);
    expect(vi.getTimerCount()).toBe(0);
  });
});
