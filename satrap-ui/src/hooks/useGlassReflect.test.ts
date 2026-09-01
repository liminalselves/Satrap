import { describe, expect, it } from 'vitest';
import { updateReflectState } from './useGlassReflect';

describe('updateReflectState', () => {
  it('仅在坐标或视觉状态变化时写入 DOM', () => {
    const styleWrites: string[] = [];
    const classWrites: string[] = [];
    const classes = new Set<string>();
    const element = {
      style: {
        setProperty: (name: string, value: string) => styleWrites.push(`${name}:${value}`),
      },
      classList: {
        add: (...names: string[]) => {
          names.forEach((name) => classes.add(name));
          classWrites.push(`add:${names.join(',')}`);
        },
        remove: (...names: string[]) => {
          names.forEach((name) => classes.delete(name));
          classWrites.push(`remove:${names.join(',')}`);
        },
      },
    } as unknown as HTMLElement;
    const item: Parameters<typeof updateReflectState>[0] = {
      element,
      reflectRange: 80,
      reflectSize: 150,
      rect: { left: 100, top: 100, right: 300, bottom: 200 },
      visible: true,
      cellKeys: [],
      visualState: 'idle',
      lastReflectX: null,
      lastReflectY: null,
    };

    expect(updateReflectState(item, 160, 140)).toBe(true);
    expect(styleWrites).toEqual(['--mouse-x:60px', '--mouse-y:40px']);
    expect(classes.has('glass-hovering')).toBe(true);
    const initialClassWrites = classWrites.length;

    expect(updateReflectState(item, 160, 140)).toBe(true);
    expect(styleWrites).toHaveLength(2);
    expect(classWrites).toHaveLength(initialClassWrites);

    expect(updateReflectState(item, 340, 140)).toBe(true);
    expect(styleWrites).toHaveLength(3);
    expect(classes.has('glass-nearby')).toBe(true);

    expect(updateReflectState(item, 500, 140)).toBe(false);
    const clearedClassWrites = classWrites.length;
    expect(classes.size).toBe(0);
    expect(updateReflectState(item, 500, 140)).toBe(false);
    expect(styleWrites).toHaveLength(3);
    expect(classWrites).toHaveLength(clearedClassWrites);
  });
});
