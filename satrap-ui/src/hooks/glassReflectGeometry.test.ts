import { describe, expect, it } from 'vitest';
import {
  calculateReflectState,
  getPointerCellKey,
  getReflectCellKeys,
} from './glassReflectGeometry';

describe('glassReflectGeometry', () => {
  const rect = { left: 100, top: 100, right: 300, bottom: 200 };

  it('计算元素内部的指针坐标和悬浮状态', () => {
    expect(calculateReflectState(rect, 160, 140, 80)).toEqual({
      x: 60,
      y: 40,
      isHovering: true,
      isInRange: true,
    });
  });

  it('区分临近状态和范围外状态', () => {
    expect(calculateReflectState(rect, 340, 140, 80).isInRange).toBe(true);
    expect(calculateReflectState(rect, 400, 140, 80).isInRange).toBe(false);
  });

  it('元素扩展范围覆盖指针所在空间分区', () => {
    const keys = getReflectCellKeys(rect, 150);
    expect(keys).toContain(getPointerCellKey(50, 50));
    expect(keys).toContain(getPointerCellKey(350, 250));
    expect(keys).not.toContain(getPointerCellKey(800, 800));
  });
});
