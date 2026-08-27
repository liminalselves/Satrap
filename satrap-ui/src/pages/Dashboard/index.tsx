import { useEffect, useCallback, useState, useMemo } from 'react';
import { useBackendStore } from '@/stores/useBackendStore';
import { useConfigStore } from '@/stores/useConfigStore';
import { useWebSocket } from '@/hooks/useWebSocket';
import { controlApi } from '@/api/control';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { toast } from '@/components/ui/Toast';
import { PageHeader, StatCard, StatCardGrid, AlertCard } from '@/components/common';
import {
  Server,
  Cpu,
  MessageSquare,
  Globe,
  RefreshCw,
  RotateCcw,
  Wifi,
  WifiOff,
  Play,
  Square,
  AlertCircle,
} from 'lucide-react';

export function Dashboard() {
  const { 
    health, 
    loading, 
    isRunning,
    controlStatus,
    refreshHealth, 
    refreshControlStatus,
    reloadConfig, 
    shutdown, 
    setHealth,
  } = useBackendStore();
  const { llmConfigs, embeddingConfigs, rerankConfigs, sessionClasses, fetchAllModels, fetchSessionClasses } = useConfigStore();
  
  const [controlLoading, setControlLoading] = useState(false);
  const { connect, disconnect, isConnected } = useWebSocket('/ws/status');

  // WebSocket 状态更新处理
  const handleStatus = useCallback((data: {
    running: boolean;
    adapters: Record<string, {
      status: string;
      started: boolean;
      config_type?: string;
      session_type?: string;
      type?: string;
      last_error?: string;
    }>;
  }) => {
    setHealth({
      running: data.running,
      adapters: data.adapters,
    });
  }, [setHealth]);

  // 连接 WebSocket
  useEffect(() => {
    connect({
      onStatus: handleStatus,
      onError: (msg) => console.error('WebSocket error:', msg),
    });
    return () => disconnect();
  }, [connect, disconnect, handleStatus]);

  // 初始加载
  useEffect(() => {
    refreshHealth();
    refreshControlStatus();
    fetchAllModels();
    fetchSessionClasses();
    
    const interval = setInterval(refreshControlStatus, 5000);
    return () => clearInterval(interval);
  }, [refreshHealth, refreshControlStatus, fetchAllModels, fetchSessionClasses]);

  // 统计数据
  const stats = useMemo(() => ({
    adapterCount: health?.adapters ? Object.keys(health.adapters).length : 0,
    modelCount: Object.keys(llmConfigs).length + Object.keys(embeddingConfigs).length + Object.keys(rerankConfigs).length,
    sessionCount: Object.keys(sessionClasses).length,
  }), [health?.adapters, llmConfigs, embeddingConfigs, rerankConfigs, sessionClasses]);

  // 操作处理函数
  const handleReload = useCallback(async () => {
    const ok = await reloadConfig();
    toast(ok ? 'success' : 'error', ok ? '配置已重载' : '重载失败');
  }, [reloadConfig]);

  const handleShutdown = useCallback(async () => {
    const ok = await shutdown();
    toast(ok ? 'success' : 'error', ok ? '后端已停止' : '停止失败');
  }, [shutdown]);

  const handleStart = useCallback(async () => {
    setControlLoading(true);
    try {
      const result = await controlApi.start();
      if (result.ok) {
        toast('success', result.message || '后端启动中');
        setTimeout(() => {
          refreshHealth();
          refreshControlStatus();
        }, 2000);
      } else {
        toast('error', result.error || '启动失败');
      }
    } catch {
      toast('error', '控制服务未运行，请先启动控制服务');
    } finally {
      setControlLoading(false);
    }
  }, [refreshHealth, refreshControlStatus]);

  const handleStop = useCallback(async () => {
    setControlLoading(true);
    try {
      const result = await controlApi.stop();
      if (result.ok) {
        toast('success', result.message || '后端已停止');
        setTimeout(() => {
          refreshHealth();
          refreshControlStatus();
        }, 1000);
      } else {
        toast('error', result.error || '停止失败');
      }
    } catch {
      await handleShutdown();
    } finally {
      setControlLoading(false);
    }
  }, [refreshHealth, refreshControlStatus, handleShutdown]);

  const handleRestart = useCallback(async () => {
    setControlLoading(true);
    try {
      const result = await controlApi.restart();
      if (result.ok) {
        toast('success', result.message || '后端重启中');
        setTimeout(() => {
          refreshHealth();
          refreshControlStatus();
        }, 3000);
      } else {
        toast('error', result.error || '重启失败');
      }
    } catch {
      toast('error', '控制服务未运行');
    } finally {
      setControlLoading(false);
    }
  }, [refreshHealth, refreshControlStatus]);

  // 页面头部操作按钮
  const headerActions = useMemo(() => (
    <>
      <Badge variant={isConnected ? 'success' : 'default'} className="flex items-center gap-1">
        {isConnected ? <Wifi className="h-3 w-3" /> : <WifiOff className="h-3 w-3" />}
        {isConnected ? '实时' : '离线'}
      </Badge>
      
      {isRunning ? (
        <>
          <Button variant="default" onClick={() => { refreshHealth(); refreshControlStatus(); }} disabled={loading}>
            <RefreshCw className={`h-4 w-4 mr-2 ${loading ? 'animate-spin' : ''}`} />
            刷新
          </Button>
          <Button variant="default" onClick={handleReload}>
            <RotateCcw className="h-4 w-4 mr-2" />
            重载配置
          </Button>
          <Button variant="default" onClick={handleRestart} disabled={controlLoading}>
            <RefreshCw className={`h-4 w-4 mr-2 ${controlLoading ? 'animate-spin' : ''}`} />
            重启
          </Button>
          <Button variant="danger" onClick={handleStop} disabled={controlLoading}>
            <Square className="h-4 w-4 mr-2" />
            停止
          </Button>
        </>
      ) : (
        <Button variant="primary" onClick={handleStart} disabled={controlLoading}>
          <Play className={`h-4 w-4 mr-2 ${controlLoading ? 'animate-pulse' : ''}`} />
          {controlLoading ? '启动中...' : '启动后端'}
        </Button>
      )}
    </>
  ), [isConnected, isRunning, loading, controlLoading, refreshHealth, refreshControlStatus, handleReload, handleRestart, handleStop, handleStart]);

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
          value={isRunning ? '运行中' : '未运行'}
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
                  <Badge variant={info.status === 'running' ? 'success' : info.status === 'error' ? 'error' : 'warning'}>
                    {info.status}
                  </Badge>
                  <Badge variant={info.started ? 'success' : 'default'}>
                    {info.started ? '已启动' : '未启动'}
                  </Badge>
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
