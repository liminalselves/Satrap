export function mergeLogSources<T extends { source?: string; timestamp?: number; id: number }>(logs: T[]): T[] {
  const sources = new Map<string, T[]>();
  for (const log of logs) {
    const key = log.source ?? '';
    const entries = sources.get(key) ?? [];
    entries.push(log); sources.set(key, entries);
  }
  const queues = [...sources.values()].map((entries) => ({ entries, index: 0 }));
  const result: T[] = [];
  while (result.length < logs.length) {
    let selected: typeof queues[number] | undefined;
    for (const queue of queues) {
      const entry = queue.entries[queue.index];
      const previous = selected?.entries[selected.index];
      if (entry && (!previous || (entry.timestamp ?? 0) < (previous.timestamp ?? 0)
        || entry.timestamp === previous.timestamp && entry.id < previous.id)) selected = queue;
    }
    if (!selected) break;
    result.push(selected.entries[selected.index++]);
  }
  return result;
}
// 按各来源的下一条时间合并, 即使宿主时钟回拨也不倒置同一来源的发布顺序

export class LogBuffer<T> {
  private ring: T[] = [];
  private next = 0;
  private version = 0;
  private snapshotVersion = -1;
  private snapshot: T[] = [];
  private frozen: T[] | null = null;
  private received = 0;

  constructor(private limit = 5000) {}

  append(item: T) {
    this.ring[this.next] = item;
    this.next = (this.next + 1) % this.limit;
    this.version++;
    if (this.frozen) this.received++;
  }

  read() {
    if (this.frozen) return { logs: this.frozen, bufferedCount: this.received };
    if (this.snapshotVersion !== this.version) {
      this.snapshot = this.ring.length < this.limit || this.next === 0
        ? this.ring.slice() : [...this.ring.slice(this.next), ...this.ring.slice(0, this.next)];
      this.snapshotVersion = this.version;
    }
    return { logs: this.snapshot, bufferedCount: 0 };
  }

  pause(value: boolean) {
    if (value && !this.frozen) { this.frozen = this.read().logs; this.received = 0; }
    else if (!value) { this.frozen = null; this.received = 0; }
  }

  clear() {
    this.ring = []; this.next = 0; this.version++;
    if (this.frozen) this.frozen = [];
    this.received = 0;
  }
}
