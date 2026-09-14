import { expect, it } from 'vitest';
import { formatBytes } from './format';

it.each([
  [null, '大小未统计'], [0, '0 B'], [1023, '1023 B'],
  [1024, '1.0 KiB'], [1024 ** 2, '1.0 MiB'], [1024 ** 3, '1.0 GiB'],
] as const)('存储量 %s 显示为 %s', (value, expected) => {
  expect(formatBytes(value)).toBe(expected);
});
