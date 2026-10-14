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
