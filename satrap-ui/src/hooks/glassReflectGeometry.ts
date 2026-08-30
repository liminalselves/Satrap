/** 反光计算使用的只读矩形 */
export interface ReflectRect {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

/** 指针相对元素的位置和反光状态 */
export interface ReflectState {
  x: number;
  y: number;
  isHovering: boolean;
  isInRange: boolean;
}

export const REFLECT_SPATIAL_CELL_SIZE = 256;

/** 将浏览器矩形复制为稳定缓存, 避免后续读取实时布局对象 */
export function copyReflectRect(rect: Pick<DOMRectReadOnly, 'left' | 'top' | 'right' | 'bottom'>): ReflectRect {
  return {
    left: rect.left,
    top: rect.top,
    right: rect.right,
    bottom: rect.bottom,
  };
}

/** 根据缓存矩形计算反光状态 */
export function calculateReflectState(
  rect: ReflectRect,
  mouseX: number,
  mouseY: number,
  reflectRange: number,
): ReflectState {
  const closestX = Math.max(rect.left, Math.min(mouseX, rect.right));
  const closestY = Math.max(rect.top, Math.min(mouseY, rect.bottom));
  const distanceToEdge = Math.hypot(mouseX - closestX, mouseY - closestY);
  return {
    x: mouseX - rect.left,
    y: mouseY - rect.top,
    isHovering: mouseX >= rect.left && mouseX <= rect.right
      && mouseY >= rect.top && mouseY <= rect.bottom,
    isInRange: distanceToEdge < reflectRange,
  };
}

/** 返回指针对应的空间分区键 */
export function getPointerCellKey(x: number, y: number): string {
  return `${Math.floor(x / REFLECT_SPATIAL_CELL_SIZE)}:${Math.floor(y / REFLECT_SPATIAL_CELL_SIZE)}`;
}

/** 返回元素影响范围覆盖的全部空间分区 */
export function getReflectCellKeys(rect: ReflectRect, reflectRange: number): string[] {
  const minimumX = Math.floor((rect.left - reflectRange) / REFLECT_SPATIAL_CELL_SIZE);
  const maximumX = Math.floor((rect.right + reflectRange) / REFLECT_SPATIAL_CELL_SIZE);
  const minimumY = Math.floor((rect.top - reflectRange) / REFLECT_SPATIAL_CELL_SIZE);
  const maximumY = Math.floor((rect.bottom + reflectRange) / REFLECT_SPATIAL_CELL_SIZE);
  const keys: string[] = [];
  for (let x = minimumX; x <= maximumX; x += 1) {
    for (let y = minimumY; y <= maximumY; y += 1) {
      keys.push(`${x}:${y}`);
    }
  }
  return keys;
}
