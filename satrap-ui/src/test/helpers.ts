import type { InternalAxiosRequestConfig } from 'axios';

/** 手工控制的 Promise: 返回 promise 与 resolve, 用于构造迟到响应场景 */
export function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

/** 构造最小 Axios 成功响应, 供测试适配器直接返回 */
export function response(config: InternalAxiosRequestConfig, data: unknown) {
  return { config, status: 200, statusText: 'OK', headers: {}, data };
}
