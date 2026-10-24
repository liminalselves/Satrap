/** 生成 UUID v4, 兼容公网 HTTP 页面中缺少 randomUUID 的浏览器环境 */
export function createUuid(): string {
  const crypto = globalThis.crypto;
  if (typeof crypto?.randomUUID === 'function') return crypto.randomUUID();
  if (typeof crypto?.getRandomValues !== 'function') throw new Error('当前浏览器不支持安全随机数, 请更新浏览器');
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;   // 固定 UUID v4 版本
  bytes[8] = (bytes[8] & 0x3f) | 0x80;   // 固定 RFC 4122 变体
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
