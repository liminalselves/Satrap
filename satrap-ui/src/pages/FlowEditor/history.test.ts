import { describe, expect, it } from 'vitest';
import { History } from './history';

describe('History', () => {
  it('undoes and redoes in order, and a new record clears redo', () => {
    const history = new History<number>(50, 0, () => 0);
    history.record(1);
    history.record(2);
    expect(history.undo(3)).toBe(2);
    expect(history.undo(2)).toBe(1);
    expect(history.undo(1)).toBeNull();
    expect(history.redo(1)).toBe(2);
    history.record(2);
    expect(history.canRedo).toBe(false);
  });

  it('merges same-key edits inside the window and drops entries past the depth', () => {
    let now = 0;
    const history = new History<string>(3, 800, () => now);
    history.record('a', 'title');
    now = 500;
    history.record('ab', 'title');
    expect(history.undo('abc')).toBe('a');
    expect(history.canUndo).toBe(false);

    for (const value of ['1', '2', '3', '4']) { now += 1000; history.record(value); }
    expect(history.undo('5')).toBe('4');
    expect(history.undo('4')).toBe('3');
    expect(history.undo('3')).toBe('2');
    expect(history.undo('2')).toBeNull();
  });
});
