import { useEffect, useMemo } from 'react';
import { useBackendStore } from '@/stores/useBackendStore';
import { useConfigStore } from '@/stores/useConfigStore';
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
  const { health, isRunning, runState, controlStatus, statusConnected } = useBackendStore();
  const { llmConfigs, embeddingConfigs, rerankConfigs, asrConfigs, sessionClasses, fetchAllModels, fetchSessionClasses } = useConfigStore();

  useEffect(() => {
    void fetchAllModels();
    void fetchSessionClasses();
  }, [fetchAllModels, fetchSessionClasses]);

  // 统计数据
  const stats = useMemo(() => ({
    adapterCount: isRunning && health?.adapters ? Object.keys(health.adapters).length : 0,
    modelCount: Object.keys(llmConfigs).length + Object.keys(embeddingConfigs).length + Object.keys(rerankConfigs).length + Object.keys(asrConfigs).length,
    sessionCount: Object.keys(sessionClasses).length,
  }), [isRunning, health?.adapters, llmConfigs, embeddingConfigs, rerankConfigs, asrConfigs, sessionClasses]);

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
          title="控制服务未运行"
          description={
            <>
              <p>
                请运行 <code className="px-2 py-1 rounded bg-glass text-accent">python -m satrap.core.backend.control_server</code> 启动控制服务
              </p>
              <p className="text-text-tertiary text-sm mt-2">
                或者手动运行 <code className="px-2 py-1 rounded bg-glass text-accent">python -m satrap.main run</code> 启动后端
              </p>
            </>
          }
        />
      )}

      {/* 状态卡片 */}
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
      {!isRunning && controlStatus && (
        <AlertCard
          variant="warning"
          icon={<Server className="h-6 w-6 text-warning" />}
          title="后端未运行"
          description={
            <>
              <p>点击上方「启动后端」按钮启动服务</p>
              {health?.error && (
                <p className="text-error text-sm mt-2">错误: {health.error}</p>
              )}
            </>
          }
        />
      )}
    </div>
  );
}
