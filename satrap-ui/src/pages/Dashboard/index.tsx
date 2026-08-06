import { useEffect, useCallback } from 'react';
import { useBackendStore } from '@/stores/useBackendStore';
import { useConfigStore } from '@/stores/useConfigStore';
import { useWebSocket } from '@/hooks/useWebSocket';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { toast } from '@/components/ui/Toast';
import {
  Server,
  Cpu,
  MessageSquare,
  Globe,
  RefreshCw,
  Power,
  RotateCcw,
  Wifi,
  WifiOff,
} from 'lucide-react';

export function Dashboard() {
  const { health, loading, refreshHealth, reloadConfig, shutdown, setHealth } = useBackendStore();
  const { llmConfigs, embeddingConfigs, rerankConfigs, sessionClasses, fetchAllModels, fetchSessionClasses } = useConfigStore();

  // WebSocket 状态推送
  const { connect, disconnect, isConnected } = useWebSocket('/ws/status');

  // 处理状态更新
  const handleStatus = useCallback((data: {
    running: boolean;
    adapters: Record<string, {
      status: string;
      started: boolean;
      config_type?: string;
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

    return () => {
      disconnect();
    };
  }, [connect, disconnect, handleStatus]);

  // 初始加载配置
  useEffect(() => {
    refreshHealth();
    fetchAllModels();
    fetchSessionClasses();
  }, [refreshHealth, fetchAllModels, fetchSessionClasses]);

  const handleReload = async () => {
    const ok = await reloadConfig();
    if (ok) {
      toast('success', '配置已重载');
    } else {
      toast('error', '重载失败');
    }
  };

  const handleShutdown = async () => {
    const ok = await shutdown();
    if (ok) {
      toast('success', '后端已停止');
    } else {
      toast('error', '停止失败');
    }
  };

  const adapterCount = health?.adapters ? Object.keys(health.adapters).length : 0;
  const modelCount = Object.keys(llmConfigs).length + Object.keys(embeddingConfigs).length + Object.keys(rerankConfigs).length;
  const sessionCount = Object.keys(sessionClasses).length;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-text-primary">仪表盘</h1>
          <p className="text-text-secondary mt-1">系统运行状态概览</p>
        </div>
        <div className="flex items-center gap-2">
          <Badge variant={isConnected ? 'success' : 'default'} className="flex items-center gap-1">
            {isConnected ? <Wifi className="h-3 w-3" /> : <WifiOff className="h-3 w-3" />}
            {isConnected ? '实时' : '离线'}
          </Badge>
          <Button variant="ghost" onClick={refreshHealth} disabled={loading}>
            <RefreshCw className={`h-4 w-4 mr-2 ${loading ? 'animate-spin' : ''}`} />
            刷新
          </Button>
          {health?.running && (
            <>
              <Button variant="default" onClick={handleReload}>
                <RotateCcw className="h-4 w-4 mr-2" />
                重载配置
              </Button>
              <Button variant="danger" onClick={handleShutdown}>
                <Power className="h-4 w-4 mr-2" />
                停止后端
              </Button>
            </>
          )}
        </div>
      </div>

      {/* 状态卡片 - 染色玻璃效果 */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        {/* 后端状态 - 蓝色染色玻璃 */}
        <Card variant="accent" interactive>
          <div className="flex items-center gap-4">
            <div className={`p-3 rounded-lg ${health?.running ? 'bg-accent-subtle' : 'bg-glass-error'}`}>
              <Server className={`h-6 w-6 ${health?.running ? 'text-accent' : 'text-error'}`} />
            </div>
            <div>
              <p className="text-sm text-text-secondary">后端状态</p>
              <p className="text-xl font-semibold text-text-primary">
                {health?.running ? '运行中' : '未运行'}
              </p>
            </div>
          </div>
        </Card>

        {/* 模型配置 - 紫色染色玻璃 */}
        <Card variant="purple" interactive>
          <div className="flex items-center gap-4">
            <div className="p-3 rounded-lg bg-purple-subtle">
              <Cpu className="h-6 w-6 text-purple" />
            </div>
            <div>
              <p className="text-sm text-text-secondary">模型配置</p>
              <p className="text-xl font-semibold text-text-primary">{modelCount}</p>
            </div>
          </div>
        </Card>

        {/* 会话类 - 青色染色玻璃 */}
        <Card variant="teal" interactive>
          <div className="flex items-center gap-4">
            <div className="p-3 rounded-lg bg-teal-subtle">
              <MessageSquare className="h-6 w-6 text-teal" />
            </div>
            <div>
              <p className="text-sm text-text-secondary">会话类</p>
              <p className="text-xl font-semibold text-text-primary">{sessionCount}</p>
            </div>
          </div>
        </Card>

        {/* 适配器 - 粉色染色玻璃 */}
        <Card variant="pink" interactive>
          <div className="flex items-center gap-4">
            <div className="p-3 rounded-lg bg-pink-subtle">
              <Globe className="h-6 w-6 text-pink" />
            </div>
            <div>
              <p className="text-sm text-text-secondary">适配器</p>
              <p className="text-xl font-semibold text-text-primary">{adapterCount}</p>
            </div>
          </div>
        </Card>
      </div>

      {/* 适配器列表 */}
      {health?.running && health.adapters && Object.keys(health.adapters).length > 0 && (
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
      {!health?.running && (
        <Card variant="warning">
          <div className="flex items-center gap-4">
            <div className="p-3 rounded-lg bg-glass-warning">
              <Server className="h-6 w-6 text-warning" />
            </div>
            <div className="flex-1">
              <h3 className="text-lg font-semibold text-text-primary">后端未运行</h3>
              <p className="text-text-secondary mt-1">
                请运行 <code className="px-2 py-1 rounded bg-glass text-accent">python -m satrap.main run</code> 启动后端服务
              </p>
              {health?.error && (
                <p className="text-error text-sm mt-2">错误: {health.error}</p>
              )}
            </div>
          </div>
        </Card>
      )}
    </div>
  );
}
