import { afterEach, describe, expect, it } from 'vitest';
import { clearCachedState, readCached, writeCached } from './useCachedState';

describe('useCachedState cache', () => {
  afterEach(() => clearCachedState());

  it('returns the fallback until a value is written', () => {
    expect(readCached('dashboard:overview', null)).toBeNull();
    writeCached('dashboard:overview', { total: 3 }, null);
    expect(readCached('dashboard:overview', null)).toEqual({ total: 3 });
  });

  it('applies functional updates against the cached value', () => {
    writeCached<number>('count', 2, 0);
    expect(writeCached<number>('count', (previous) => previous + 1, 0)).toBe(3);
    expect(writeCached<number>('fresh', (previous) => previous + 1, 10)).toBe(11);
  });

  it('keeps keys independent and keeps an explicit null', () => {
    writeCached('usage:today', 'a', '');
    writeCached('usage:7d', 'b', '');
    writeCached<string | null>('usage:30d', null, 'fallback');
    expect(readCached('usage:today', '')).toBe('a');
    expect(readCached('usage:7d', '')).toBe('b');
    expect(readCached<string | null>('usage:30d', 'fallback')).toBeNull();
  });
});
