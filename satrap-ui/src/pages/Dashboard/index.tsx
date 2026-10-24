import { useEffect, useMemo, useState } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { useBackendStore } from '@/stores/useBackendStore';
import { controlClient } from '@/api/control';
import { startVisiblePoll } from '@/utils/visiblePoll';
import { Card } from '@/components/ui/Card';
import { Badge } from '@/components/ui/Badge';
import { BackendControls } from '@/components/common/BackendControls';
import { AdapterStatus } from '@/components/common/AdapterStatus';
import { PageHeader, StatCard, StatCardGrid, AlertCard } from '@/components/common';
import {
  Server,
  Cpu,
  MessageSquare,
  Globe,
  Wifi,
  WifiOff,
  AlertCircle,
} from 'lucide-react';

export function Dashboard() {
  const [counts, setCounts] = useState({ modelCount: 0, sessionCount: 0 });
  const [configError, setConfigError] = useState('');
  useEffect(() => {
    const poll = startVisiblePoll(async (signal) => {
      const { data } = await controlClient.get<{ model_counts: Record<string, number>; session_count: number }>('/config/overview', { signal });
      if (signal.aborted) return;
      setCounts({ modelCount: Object.values(data.model_counts).reduce((total, count) => total + count, 0), sessionCount: data.session_count });
      setConfigError('');
    }, { interval: 30000, onError: (error) => setConfigError(error instanceof Error ? error.message : '读取配置概览失败') });
    return () => poll.stop();
  }, []);
  const { health, healthAvailable, isRunning, runState, controlStatus, statusConnected } = useBackendStore(useShallow((state) => ({
    health: state.health, healthAvailable: state.healthAvailable, isRunning: state.isRunning, runState: state.runState, controlStatus: state.controlStatus, statusConnected: state.statusConnected })));

  // 统计数据
  const stats = useMemo(() => ({
    adapterCount: isRunning && health?.adapters ? Object.keys(health.adapters).length : 0,
    ...counts,
  }), [isRunning, health?.adapters, counts]);

  const headerActions = <>
    <Badge variant={statusConnected ? 'success' : 'default'} className="flex items-center gap-1">
      {statusConnected ? <Wifi className="h-3 w-3" /> : <WifiOff className="h-3 w-3" />}
      {statusConnected ? '实时' : '轮询'}
    </Badge>
    <BackendControls />
  </>;

  return (
    <div className="space-y-6">
      <PageHeader
        title="仪表盘"
        description="系统运行状态概览"
        actions={headerActions}
      />

      {/* 控制服务状态提示 */}
      {!controlStatus && !isRunning && (
        <AlertCard
          variant="warning"
          icon={<AlertCircle className="h-6 w-6 text-warning" />}
          title="暂时无法确认后端状态"
          description={
            <>
              <p>
                请检查控制服务是否已启动及连接地址是否正确。启动命令: <code className="px-2 py-1 rounded bg-glass text-accent">python -m satrap.core.backend.control_server</code>
              </p>
              <p className="text-text-tertiary text-sm mt-2">
                或者手动运行 <code className="px-2 py-1 rounded bg-glass text-accent">python -m satrap.main run</code> 启动后端
              </p>
            </>
          }
        />
      )}

      {/* 状态卡片 */}
      {configError && <p role="alert" className="text-error">{configError}</p>}
      <StatCardGrid>
        <StatCard
          variant="accent"
          icon={<Server className="h-6 w-6 text-accent" />}
          label="后端状态"
          value={runState === 'unknown' ? '状态未知' : isRunning ? '运行中' : '未运行'}
        />
        <StatCard
          variant="purple"
          icon={<Cpu className="h-6 w-6 text-purple" />}
          label="模型配置"
          value={stats.modelCount}
        />
        <StatCard
          variant="teal"
          icon={<MessageSquare className="h-6 w-6 text-teal" />}
          label="会话类"
          value={stats.sessionCount}
        />
        <StatCard
          variant="pink"
          icon={<Globe className="h-6 w-6 text-pink" />}
          label="适配器"
          value={stats.adapterCount}
        />
      </StatCardGrid>

      {/* 适配器列表 */}
      {isRunning && health?.adapters && Object.keys(health.adapters).length > 0 && (
        <Card>
          <h3 className="text-lg font-semibold text-text-primary mb-4">运行中的适配器</h3>
          <div className="space-y-3">
            {Object.entries(health.adapters).map(([id, info]) => (
              <div
                key={id}
                className="flex items-center justify-between p-3 rounded-lg bg-glass hover:bg-glass-hover transition-colors"
              >
                <div className="flex items-center gap-3">
                  <span className="font-medium text-text-primary">{id}</span>
                  <Badge variant="info">{info.config_type || info.type || 'unknown'}</Badge>
                </div>
                <div className="flex items-center gap-2">
                  <AdapterStatus info={info} application={health.platform_config?.find((item) => item.id === id)} />
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}

      {/* 后端未运行提示 */}
      {runState === 'stopped' && controlStatus && (
        <AlertCard
          variant="warning"
          icon={<Server className="h-6 w-6 text-warning" />}
          title="后端未运行"
          description={
            <>
              <p>点击上方「启动后端」按钮启动服务</p>
            </>
          }
        />
      )}
      {isRunning && !healthAvailable && health?.error && (
        <AlertCard variant="warning" icon={<AlertCircle className="h-6 w-6 text-warning" />}
          title="后端已启动, 暂时无法读取运行状态" description={health.error} />
      )}
    </div>
  );
}
