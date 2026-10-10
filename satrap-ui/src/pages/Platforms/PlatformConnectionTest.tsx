import { useEffect, useRef, useState } from 'react';
import { Modal } from '@/components/ui/Modal';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { backendApi } from '@/api/backend';
import type { ConnectionProbeResult } from '@/api/backend';
import type { PlatformConfig } from '@/api/types';

export function PlatformConnectionTest({ platform, revision, identity, onClose }: {
  platform: PlatformConfig | null; revision: string; identity: string; onClose: () => void;
}) {
  const [result, setResult] = useState<ConnectionProbeResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [invalidated, setInvalidated] = useState(false);
  const run = useRef(0);
  const signature = `${platform?.id || ''}\u0000${revision}\u0000${identity}`;
  const previousSignature = useRef(signature);
  useEffect(() => {
    ++run.current;
    setBusy(false);
    setResult(null);
    setInvalidated(Boolean(previousSignature.current.split('\u0000')[0]) && previousSignature.current !== signature && Boolean(platform));
    previousSignature.current = signature;
    return () => { ++run.current; };
  }, [signature, platform]);
  const test = async () => {
    if (!platform || busy) return;
    const current = ++run.current;
    setBusy(true);
    setResult(null);
    setInvalidated(false);
    const started = Date.now();
    try {
      const response = await backendApi.checkPlatformConnection(platform.id);
      if (run.current === current) setResult(response);
    } catch (error) {
      if (run.current === current) setResult({ ok: false,
        detail: error instanceof Error ? error.message : '无法连接后端', elapsed_ms: Date.now() - started });
    } finally {
      if (run.current === current) setBusy(false);
    }
  };
  return <Modal open={platform !== null} onClose={onClose} title="通信检查" size="lg">
    <div className="space-y-4">
      <p className="break-all text-sm">平台: {platform?.id}</p>
      <p className="text-sm text-text-secondary">向当前连接的适配器或平台发起一次只读请求, 确认是否正常响应</p>
      <div className="flex gap-2">
        <Button variant="primary" onClick={() => void test()} disabled={busy}>{busy ? '检查中...' : '开始检查'}</Button>
        <Button variant="default" onClick={onClose}>关闭检查</Button>
      </div>
      {invalidated && <p role="status" className="text-sm text-warning">平台或配置已变化, 旧结果已清除, 请重新测试</p>}
      <div className="space-y-3" aria-live="polite" aria-busy={busy}>
        {result && <div className="rounded-sm bg-glass p-3" role="status">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={result.ok ? 'success' : 'error'}>
              {result.ok ? '通信正常' : '通信检查失败'}
            </Badge>
            <span className="text-xs text-text-secondary">{result.elapsed_ms} ms</span>
          </div>
          <p className="mt-1 break-words text-sm text-text-secondary">{result.detail}</p>
        </div>}
      </div>
    </div>
  </Modal>;
}
