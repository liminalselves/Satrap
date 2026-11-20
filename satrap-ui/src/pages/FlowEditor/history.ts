/**
 * 撤销/重做栈
 *
 * 每次变更前调用 record 保存变更前的快照; 同一 key 在 mergeMs 内的连续变更 (如输入框逐字输入) 合并为一步
 */
export class History<T> {
  private past: T[] = [];
  private future: T[] = [];
  private lastKey: string | null = null;
  private lastAt = 0;

  constructor(private readonly depth = 50, private readonly mergeMs = 800, private readonly now = () => Date.now()) {}

  record(snapshot: T, key?: string): void {
    const at = this.now();
    if (key && key === this.lastKey && at - this.lastAt < this.mergeMs) {
      this.lastAt = at;
      return;
    }
    this.past.push(snapshot);
    if (this.past.length > this.depth) this.past.shift();
    this.future = [];
    this.lastKey = key ?? null;
    this.lastAt = at;
  }

  /** 返回要恢复的快照; current 进入重做栈 */
  undo(current: T): T | null {
    const previous = this.past.pop();
    if (previous === undefined) return null;
    this.future.push(current);
    this.lastKey = null;
    return previous;
  }

  redo(current: T): T | null {
    const next = this.future.pop();
    if (next === undefined) return null;
    this.past.push(current);
    this.lastKey = null;
    return next;
  }

  clear(): void {
    this.past = [];
    this.future = [];
    this.lastKey = null;
  }

  get canUndo(): boolean { return this.past.length > 0; }
  get canRedo(): boolean { return this.future.length > 0; }
}
