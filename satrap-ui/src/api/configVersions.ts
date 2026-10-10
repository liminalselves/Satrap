import axios from 'axios';
import { controlClient } from './control';
import { getControlApiUrl } from '@/utils/constants';

export type ConfigResource = 'main_config' | 'models' | 'session_classes' | 'edictum_configs'
  | 'chat_plugins' | 'plugin_catalog' | 'plugin_defaults';
export interface ConfigVersion { change_token: string; state: 'available' | 'missing' | 'damaged' | 'unreadable' }
export type ConfigVersions = Partial<Record<ConfigResource, ConfigVersion>>;

export async function getConfigVersions(resources: readonly ConfigResource[], force = false, signal?: AbortSignal): Promise<ConfigVersions | null> {
  if (!resources.length) return null; // 无版本来源的读取仅合并在途请求, 不缓存正文
  try {
    const { data } = await controlClient.get('/config/versions', { params: { resources: resources.join(','), ...(force ? { force: true } : {}) }, signal });
    if (!data || data.ok !== true || typeof data.resources !== 'object' || !data.resources) throw new Error('配置版本响应格式无效');
    for (const name of resources) {
      const entry = data.resources[name];
      if (!entry || typeof entry.change_token !== 'string' || !entry.change_token
        || !['available', 'missing', 'damaged', 'unreadable'].includes(entry.state)) throw new Error('配置版本响应缺少有效来源: ' + name);
      if (entry.state === 'damaged' || entry.state === 'unreadable') throw new Error('配置来源' + (entry.state === 'damaged' ? '损坏' : '不可读') + ': ' + name);
    }
    return data.resources as ConfigVersions;
  } catch (error) {
    if (axios.isAxiosError(error) && error.response?.status === 404) return null; // 旧控制服务没有版本接口时仍实时读取正文
    throw error;
  }
}

export function sameConfigVersions(left: ConfigVersions | null, right: ConfigVersions | null, resources: readonly ConfigResource[]): boolean {
  return left !== null && right !== null && resources.every((name) => left[name]?.change_token === right[name]?.change_token && left[name]?.state === right[name]?.state);
}

interface Flight<T> { scope: string; epoch: number; controller: AbortController; promise: Promise<T>; users: number; settled: boolean }
export interface ConfigReadOptions { force?: boolean; signal?: AbortSignal; versions?: ConfigVersions | null }

export class VersionedConfigRead<T> {
  private epoch = 0;
  private cached?: { scope: string; versions: ConfigVersions; data: T };
  private flight?: Flight<T>;

  constructor(private resources: readonly ConfigResource[], private loader: (signal: AbortSignal) => Promise<T>) {}

  invalidate() {
    this.epoch++;
    this.cached = undefined;
    this.flight = undefined;
  }

  read({ force = false, signal, versions }: ConfigReadOptions = {}): Promise<T> {
    if (signal?.aborted) return Promise.reject(new DOMException('读取已取消', 'AbortError'));
    if (force) this.invalidate();
    const scope = getControlApiUrl();
    let flight = this.flight;
    if (!flight || flight.scope !== scope || flight.controller.signal.aborted) {
      const controller = new AbortController();
      flight = { scope, epoch: this.epoch, controller, users: 0, settled: false, promise: Promise.resolve(undefined as T) };
      const current = flight;
      current.promise = this.load(current, force, versions).finally(() => {
        current.settled = true;
        if (this.flight === current) this.flight = undefined;
      });
      this.flight = current;
    }
    return this.join(flight, signal);
  }

  private async load(flight: Flight<T>, force: boolean, hint?: ConfigVersions | null): Promise<T> {
    const signal = flight.controller.signal;
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        const before = attempt === 0 && hint !== undefined && !force ? hint : await getConfigVersions(this.resources, force && attempt === 0, signal);
        if (flight.scope !== getControlApiUrl()) throw new Error('控制服务地址已改变, 已丢弃旧读取结果');
        if (signal.aborted) throw new DOMException('读取已取消', 'AbortError');
        const cached = this.cached;
        if (cached?.scope === flight.scope && sameConfigVersions(cached.versions, before, this.resources)) return cached.data;
        const data = await this.loader(signal);
        if (flight.scope !== getControlApiUrl()) throw new Error('控制服务地址已改变, 已丢弃旧读取结果');
        if (signal.aborted) throw new DOMException('读取已取消', 'AbortError');
        if (before === null) return data;
        const after = await getConfigVersions(this.resources, false, signal);
        if (flight.scope !== getControlApiUrl()) throw new Error('控制服务地址已改变, 已丢弃旧读取结果');
        if (!sameConfigVersions(before, after, this.resources)) continue;
        if (flight.epoch === this.epoch && flight.scope === getControlApiUrl() && !signal.aborted && after) this.cached = { scope: flight.scope, versions: after, data };
        return data;
      } catch (error) {
        if (axios.isAxiosError(error) && error.response?.status === 409 && error.response.data?.code === 'config_version_changed') continue;
        throw error;
      }
    }
    throw new Error('配置在读取期间持续变化, 请稍后重试');
  }

  private join(flight: Flight<T>, signal?: AbortSignal): Promise<T> {
    flight.users++;
    return new Promise<T>((resolve, reject) => {
      let done = false;
      const release = () => {
        if (done) return false;
        done = true;
        signal?.removeEventListener('abort', abort);
        if (--flight.users === 0 && !flight.settled) {
          if (this.flight === flight) this.flight = undefined;
          flight.controller.abort();
        }
        return true;
      };
      const abort = () => { if (release()) reject(new DOMException('读取已取消', 'AbortError')); };
      signal?.addEventListener('abort', abort, { once: true });
      flight.promise.then((data) => { if (release()) resolve(data); }, (error: unknown) => { if (release()) reject(error); });
    });
  }
}
