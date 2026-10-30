import { useEffect, useRef, useState } from 'react';
import { Activity, Cable, Play, RefreshCw } from 'lucide-react';
import { Badge, Button, Modal } from '@/components/kit';
import { backendApi } from '@/api/backend';
import type { ConnectionProbeResult } from '@/api/backend';
import type { PlatformConfig } from '@/api/types';
import { Note, PlatformIcon } from './shared';

/**
 * 连接测试: 只读探测当前适配器, 结果按平台/修订/运行时代次失效。
 * 文案与可访问名保持既有回归契约 (开始检查 / 关闭检查 / 通信正常 等)。
 */
export function PlatformConnectionTest({ platform, revision, identity, onClose }: {
  platform: PlatformConfig | null; revision: string; identity: string; onClose: () => void;
}) {
  const [result, setResult] = useState<ConnectionProbeResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [invalidated, setInvalidated] = useState(false);
  const [runId, setRunId] = useState(0);
  const run = useRef(0);
  const signature = `${platform?.id || ''}\u0000${revision}\u0000${identity}`;
  const previousSignature = useRef(signature);
  useEffect(() => {
    const counter = run;
    ++counter.current;
    setBusy(false);
    setResult(null);
    setRunId(0);
    setInvalidated(Boolean(previousSignature.current.split('\u0000')[0]) && previousSignature.current !== signature && Boolean(platform));
    previousSignature.current = signature;
    return () => { ++counter.current; };
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
      if (run.current === current) { setResult(response); setRunId((value) => value + 1); }
    } catch (error) {
      if (run.current === current) {
        setResult({ ok: false,
          detail: error instanceof Error ? error.message : '无法连接后端', elapsed_ms: Date.now() - started });
        setRunId((value) => value + 1);
      }
    } finally {
      if (run.current === current) setBusy(false);
    }
  };
  return (
    <Modal open={platform !== null} onClose={onClose} title="通信检查" icon={<Cable className="h-[18px] w-[18px]" />}
      description="向当前连接的适配器或平台发起一次只读请求, 确认是否正常响应" size="lg" className="tint-pink"
      footer={<>
        <Button variant="ghost" onClick={onClose}>关闭检查</Button>
        <Button variant="primary" icon={<Play className="h-4 w-4" />} loading={busy} onClick={() => void test()}>
          {busy ? '检查中...' : '开始检查'}
        </Button>
      </>}>
      {platform && (
        <div className="space-y-4">
          <div className="flex items-center gap-3 rounded-xl border border-line bg-glass p-3">
            <PlatformIcon type={platform.type} />
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium text-fg">{platform.id}</span>
                <Badge tone="pink">{platform.type}</Badge>
              </div>
              <p className="num mt-0.5 font-mono text-[11.5px] text-fg-3">修订 {revision || '未知'}</p>
            </div>
          </div>
          <Note tone="pink" icon={<Activity className="h-4 w-4" />} title="只读探测">
            不会发送群消息或触发模型; 探测结果用于确认协议握手与账号状态。
          </Note>
          {invalidated && <Note tone="orange">平台或配置已变化, 旧结果已清除, 请重新测试</Note>}
          <div aria-live="polite" aria-busy={busy} className="min-h-[86px]">
            {busy && (
              <div className="space-y-2 rounded-xl border border-line bg-glass p-4">
                <div className="flex items-center gap-2 text-[13px] text-fg-2"><RefreshCw className="h-4 w-4 animate-spin-slow text-pink" />正在与适配器握手…</div>
                <div className="h-1.5 overflow-hidden rounded-full bg-glass-hover">
                  <div className="h-full w-1/2 animate-pulse rounded-full bg-[linear-gradient(90deg,transparent,rgb(var(--rgb-pink)),transparent)]" />
                </div>
              </div>
            )}
            {!busy && result && (
              <div key={runId} className="rounded-xl border border-line bg-glass p-4 animate-pop" role="status">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone={result.ok ? 'green' : 'red'} dot>{result.ok ? '通信正常' : '通信检查失败'}</Badge>
                  <span className="num text-[12px] text-fg-3">{result.elapsed_ms} ms</span>
                </div>
                <p className="mt-2 break-words text-[13px] leading-5 text-fg-2">{result.detail}</p>
              </div>
            )}
            {!busy && !result && (
              <div className="flex h-[86px] items-center justify-center rounded-xl border border-dashed border-[var(--glass-border-strong)] text-[12.5px] text-fg-4">
                尚未检查。点击「开始检查」发起一次只读探测。
              </div>
            )}
          </div>
        </div>
      )}
    </Modal>
  );
}
