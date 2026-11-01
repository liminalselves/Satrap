import type { ChatGenerationSettings } from '@/api/chat';

interface TemperatureEntry {
  // 每次用户修改递增, 只允许最新代次回写界面或提示失败
  generation: number;
  // 最近一次服务端确认的落库值, 失败时回退到该值而不是修改前的瞬时值
  confirmed: ChatGenerationSettings | null;
  // 在途期间只保留最新待发送值
  pending: { value: number | null; generation: number } | null;
  sending: boolean;
}

export interface TemperatureSyncCallbacks {
  save: (conversationId: string, value: number | null) => Promise<ChatGenerationSettings>;
  onApplied?: (conversationId: string, result: ChatGenerationSettings) => void;
  onRestored?: (conversationId: string, confirmed: ChatGenerationSettings | null) => void;
  onError?: (conversationId: string, error: unknown) => void;
}

/**
 * 按会话串行同步温度修改
 *
 * 每个会话至多一个在途 PATCH, 在途时仅保留最新待发送值, 完成后继续发送;
 * 响应与失败只对最新代次生效, 会话之间互不影响, 最终值一定会发送
 */
export class TemperatureSync {
  private entries = new Map<string, TemperatureEntry>();
  private callbacks: TemperatureSyncCallbacks;

  constructor(callbacks: TemperatureSyncCallbacks) {
    this.callbacks = callbacks;
  }

  private entry(conversationId: string): TemperatureEntry {
    let entry = this.entries.get(conversationId);
    if (!entry) {
      entry = { generation: 0, confirmed: null, pending: null, sending: false };
      this.entries.set(conversationId, entry);
    }
    return entry;
  }

  /** 记录服务端已确认的会话温度, 供失败回退使用 */
  seed(conversationId: string, confirmed: ChatGenerationSettings): void {
    const entry = this.entry(conversationId);
    if (!entry.sending && !entry.pending) entry.confirmed = confirmed;
  }

  /** 判断该会话是否仍有未落库的温度修改 */
  isBusy(conversationId: string): boolean {
    const entry = this.entries.get(conversationId);
    return Boolean(entry && (entry.sending || entry.pending));
  }

  /** 提交最新温度; 同一会话的在途请求完成后自动发送最后保留的值 */
  set(conversationId: string, value: number | null, fallback?: ChatGenerationSettings): void {
    const entry = this.entry(conversationId);
    // 首次读取确认值之前发生修改时, 用修改前的界面值兜底, 失败也能回退到可解释的状态
    if (!entry.confirmed && fallback) entry.confirmed = fallback;
    const generation = entry.generation + 1;
    entry.generation = generation;
    entry.pending = { value, generation };
    void this.flush(conversationId);
  }

  private flush(conversationId: string): void {
    const entry = this.entry(conversationId);
    if (entry.sending || !entry.pending) return;
    const pending = entry.pending;
    entry.pending = null;
    entry.sending = true;
    const requestGeneration = pending.generation;

    void this.callbacks.save(conversationId, pending.value).then((result) => {
      entry.confirmed = result;
      // 已被后续修改取代时只更新落库基线, 不回写旧响应
      if (requestGeneration === entry.generation && !entry.pending) {
        this.callbacks.onApplied?.(conversationId, result);
      }
    }).catch((error: unknown) => {
      if (requestGeneration === entry.generation && !entry.pending) {
        this.callbacks.onRestored?.(conversationId, entry.confirmed);
        this.callbacks.onError?.(conversationId, error);
      }
    }).finally(() => {
      entry.sending = false;
      this.flush(conversationId);
    });
  }
}
