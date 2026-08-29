import { useState, useEffect, useCallback, useMemo } from 'react';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/Tabs';
import { toast } from '@/components/ui/Toast';
import { useBackendStore } from '@/stores/useBackendStore';
import { controlApi } from '@/api/control';
import { PageHeader, AlertCard } from '@/components/common';
import { Save, RotateCcw, Power, Play, RefreshCw, FileText, AlertCircle } from 'lucide-react';
import * as yaml from 'js-yaml';
import { DataMaintenancePanel } from './DataMaintenancePanel';

interface ConfigData {
  api?: {
    host?: string;
    port?: number;
  };
  default_session_type?: string;
  max_sessions?: number;
  idle_timeout?: number;
  llm_timeout?: number;
  rate_limit?: number;
  rate_burst?: number;
  platforms?: unknown[];
  [key: string]: unknown;
}

export function Settings() {
  const { health, isRunning, reloadConfig, shutdown, refreshAll } = useBackendStore();
  const [config, setConfig] = useState<ConfigData>({});
  const [rawConfig, setRawConfig] = useState('');
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [configPath, setConfigPath] = useState('');
  const [configExists, setConfigExists] = useState(false);
  const [controlAvailable, setControlAvailable] = useState(false);

  // 从控制服务加载配置
  const loadConfig = useCallback(async () => {
    setLoading(true);
    try {
      const result = await controlApi.getConfig();
      if (result.ok && result.config) {
        setConfig(result.config);
        setRawConfig(yaml.dump(result.config, { indent: 2 }));
        setConfigPath(result.path || '');
        setConfigExists(result.exists !== false);
        setControlAvailable(true);
      } else {
        setControlAvailable(false);
      }
    } catch {
      setControlAvailable(false);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadConfig();
  }, [loadConfig]);

  const persistConfig = useCallback(async (data: ConfigData) => {
    setSaving(true);
    try {
      const result = await controlApi.saveConfig(data);
      if (result.ok) {
        setConfig(data);
        setRawConfig(yaml.dump(data, { indent: 2 }));
        setConfigExists(true);
        if (isRunning) {
          const restarted = await controlApi.restart();
          toast(
            restarted.ok ? 'success' : 'warning',
            restarted.ok ? '配置已保存, 后端正在重启' : `配置已保存, 但重启失败: ${restarted.error || '未知错误'}`,
          );
          await refreshAll();
        } else {
          toast('success', '配置已保存');
        }
      } else {
        toast('error', result.error || '保存失败');
      }
    } catch (e) {
      toast('error', '保存失败: ' + (e instanceof Error ? e.message : '控制服务未运行'));
    } finally {
      setSaving(false);
    }
  }, [isRunning, refreshAll]);

  // 保存配置
  const handleSave = useCallback(async () => {
    await persistConfig(config);
  }, [config, persistConfig]);

  // 保存原始配置
  const handleSaveRaw = useCallback(async () => {
    setSaving(true);
    try {
      const parsed = yaml.load(rawConfig, { 
        schema: yaml.JSON_SCHEMA,
        json: true
      }) as ConfigData;
      
      if (typeof parsed !== 'object' || parsed === null) {
        toast('error', '配置必须是对象格式');
        return;
      }
      
      await persistConfig(parsed);
    } catch (e) {
      if (e instanceof yaml.YAMLException) {
        toast('error', 'YAML 格式错误: ' + e.message);
      } else {
        toast('error', '配置处理失败: ' + (e instanceof Error ? e.message : '未知错误'));
      }
    } finally {
      setSaving(false);
    }
  }, [persistConfig, rawConfig]);

  const handleCreateDefault = useCallback(async () => {
    setLoading(true);
    try {
      const result = await controlApi.createDefaultConfig();
      if (!result.ok || !result.config) throw new Error(result.error || '创建失败');
      setConfig(result.config);
      setRawConfig(yaml.dump(result.config, { indent: 2 }));
      setConfigPath(result.path || '');
      setConfigExists(true);
      toast('success', '默认配置已创建');
    } catch (e) {
      toast('error', '创建默认配置失败: ' + (e instanceof Error ? e.message : '未知错误'));
    } finally {
      setLoading(false);
    }
  }, []);

  // 重载配置
  const handleReload = useCallback(async () => {
    const ok = await reloadConfig();
    toast(ok ? 'success' : 'error', ok ? '配置已重载' : '重载失败');
  }, [reloadConfig]);

  // 停止后端
  const handleShutdown = useCallback(async () => {
    if (!confirm('确定要停止后端吗？')) return;
    const ok = await shutdown();
    toast(ok ? 'success' : 'error', ok ? '后端已停止' : '停止失败');
  }, [shutdown]);

  // 启动后端
  const handleStart = useCallback(async () => {
    try {
      const result = await controlApi.start();
      toast(result.ok ? 'success' : 'error', result.ok ? (result.message || '后端启动中') : (result.error || '启动失败'));
    } catch {
      toast('error', '控制服务未运行');
    }
  }, []);

  const handleRestart = useCallback(async () => {
    try {
      const result = await controlApi.restart();
      toast(result.ok ? 'success' : 'error', result.ok ? (result.message || '后端重启中') : (result.error || '重启失败'));
      await refreshAll();
    } catch {
      toast('error', '控制服务未运行');
    }
  }, [refreshAll]);

  // 更新配置字段
  const updateConfig = useCallback((key: string, value: unknown) => {
    setConfig((prev) => ({ ...prev, [key]: value }));
  }, []);

  const updateApiConfig = useCallback((key: string, value: unknown) => {
    setConfig((prev) => ({
      ...prev,
      api: { ...(prev.api || {}), [key]: value },
    }));
  }, []);

  // 后端地址
  const backendUrl = useMemo(() => {
    if (!health?.running || !config.api) return null;
    return `http://${config.api.host || '127.0.0.1'}:${config.api.port || 19870}`;
  }, [health?.running, config.api]);

  return (
    <div className="space-y-6">
      <PageHeader
        title="系统设置"
        description="配置系统参数和后端控制"
      />

      {/* 控制服务状态 */}
      {!controlAvailable && (
        <AlertCard
          variant="warning"
          icon={<AlertCircle className="h-6 w-6 text-warning" />}
          title="控制服务未运行"
          description={
            <p>
              请运行 <code className="px-2 py-1 rounded bg-glass text-accent">python -m satrap.core.backend.control_server</code> 启动控制服务以编辑配置
            </p>
          }
        />
      )}

      {/* 后端控制 */}
      <Card>
        <h3 className="text-lg font-semibold text-text-primary mb-4">后端控制</h3>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-4">
            <div className={`w-3 h-3 rounded-full ${health?.running ? 'bg-success' : 'bg-error'}`} />
            <span className="text-text-primary">
              {health?.running ? '后端运行中' : '后端未运行'}
            </span>
            {backendUrl && (
              <span className="text-text-secondary text-sm">{backendUrl}</span>
            )}
          </div>
          <div className="flex gap-2">
            {isRunning ? (
              <>
                <Button variant="default" onClick={handleReload}>
                  <RotateCcw className="h-4 w-4 mr-2" />
                  重载配置
                </Button>
                <Button variant="primary" onClick={handleRestart}>
                  <RefreshCw className="h-4 w-4 mr-2" />
                  重启后端
                </Button>
                <Button variant="danger" onClick={handleShutdown}>
                  <Power className="h-4 w-4 mr-2" />
                  停止后端
                </Button>
              </>
            ) : (
              <Button variant="primary" onClick={handleStart} disabled={!controlAvailable}>
                <Play className="h-4 w-4 mr-2" />
                启动后端
              </Button>
            )}
          </div>
        </div>
      </Card>

      {/* 配置编辑 */}
      <Card>
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-semibold text-text-primary">配置文件</h3>
          <div className="flex items-center gap-2">
            {configPath && (
              <span className="text-sm text-text-tertiary flex items-center gap-1">
                <FileText className="h-4 w-4" />
                {configPath}
              </span>
            )}
            <Button variant="ghost" size="sm" onClick={loadConfig} disabled={loading}>
              <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
            </Button>
          </div>
        </div>

        {!configExists && controlAvailable && (
          <div className="mb-4 flex items-center justify-between rounded-sm border border-border-glass bg-glass p-4">
            <div>
              <p className="font-medium text-text-primary">尚未创建配置文件</p>
              <p className="text-sm text-text-secondary">可以生成完整的默认配置后再编辑</p>
            </div>
            <Button variant="primary" onClick={handleCreateDefault} disabled={loading}>
              创建默认配置
            </Button>
          </div>
        )}

        <Tabs defaultValue="general">
          <TabsList>
            <TabsTrigger value="general">常用配置</TabsTrigger>
            <TabsTrigger value="raw">原始配置</TabsTrigger>
            <TabsTrigger value="data">数据维护</TabsTrigger>
            <TabsTrigger value="about">关于</TabsTrigger>
          </TabsList>

          <TabsContent value="general">
            <div className="grid grid-cols-2 gap-6">
              <div>
                <label className="block text-sm font-medium text-text-secondary mb-1">
                  API Host
                </label>
                <Input
                  value={config.api?.host || ''}
                  onChange={(e) => updateApiConfig('host', e.target.value)}
                  disabled={!controlAvailable || !configExists}
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-text-secondary mb-1">
                  API Port
                </label>
                <Input
                  type="number"
                  value={config.api?.port || 19870}
                  onChange={(e) => updateApiConfig('port', Number(e.target.value))}
                  disabled={!controlAvailable || !configExists}
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-text-secondary mb-1">
                  默认会话类型
                </label>
                <Input
                  value={config.default_session_type || ''}
                  onChange={(e) => updateConfig('default_session_type', e.target.value)}
                  disabled={!controlAvailable || !configExists}
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-text-secondary mb-1">
                  最大会话数
                </label>
                <Input
                  type="number"
                  value={config.max_sessions || 100}
                  onChange={(e) => updateConfig('max_sessions', Number(e.target.value))}
                  disabled={!controlAvailable || !configExists}
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-text-secondary mb-1">
                  会话闲置超时（秒）
                </label>
                <Input
                  type="number"
                  value={config.idle_timeout || 3600}
                  onChange={(e) => updateConfig('idle_timeout', Number(e.target.value))}
                  disabled={!controlAvailable || !configExists}
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-text-secondary mb-1">
                  LLM 超时（秒）
                </label>
                <Input
                  type="number"
                  value={config.llm_timeout || 60}
                  onChange={(e) => updateConfig('llm_timeout', Number(e.target.value))}
                  disabled={!controlAvailable || !configExists}
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-text-secondary mb-1">
                  限流速率
                </label>
                <Input
                  type="number"
                  value={config.rate_limit || 10}
                  onChange={(e) => updateConfig('rate_limit', Number(e.target.value))}
                  disabled={!controlAvailable || !configExists}
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-text-secondary mb-1">
                  限流突发数
                </label>
                <Input
                  type="number"
                  value={config.rate_burst || 20}
                  onChange={(e) => updateConfig('rate_burst', Number(e.target.value))}
                  disabled={!controlAvailable || !configExists}
                />
              </div>
            </div>

            <div className="flex gap-3 mt-6 pt-6 border-t border-border-glass">
              <Button variant="primary" onClick={handleSave} disabled={!controlAvailable || !configExists || saving}>
                <Save className="h-4 w-4 mr-2" />
                {saving ? '保存中...' : '保存配置'}
              </Button>
            </div>
          </TabsContent>

          <TabsContent value="raw">
            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-text-secondary mb-1">
                  完整配置 (YAML)
                </label>
                <textarea
                  className="glass-input w-full h-96 font-mono text-sm resize-none"
                  placeholder="# 配置文件内容"
                  value={rawConfig}
                  onChange={(e) => setRawConfig(e.target.value)}
                  disabled={!controlAvailable || !configExists}
                />
              </div>
              <div className="flex gap-3">
                <Button variant="primary" onClick={handleSaveRaw} disabled={!controlAvailable || !configExists || saving}>
                  <Save className="h-4 w-4 mr-2" />
                  {saving ? '保存中...' : '保存原始配置'}
                </Button>
              </div>
            </div>
          </TabsContent>

          <TabsContent value="data">
            <DataMaintenancePanel
              backendRunning={isRunning}
              controlAvailable={controlAvailable}
            />
          </TabsContent>

          <TabsContent value="about">
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div className="p-4 rounded-sm bg-glass">
                  <p className="text-sm text-text-tertiary">前端版本</p>
                  <p className="text-lg font-semibold text-text-primary">1.0.0</p>
                </div>
                <div className="p-4 rounded-sm bg-glass">
                  <p className="text-sm text-text-tertiary">后端 API</p>
                  <p className="text-lg font-semibold text-text-primary">
                    {health?.running ? '已连接' : '未连接'}
                  </p>
                </div>
              </div>
              <div className="p-4 rounded-sm bg-glass">
                <p className="text-sm text-text-tertiary mb-2">技术栈</p>
                <div className="flex flex-wrap gap-2">
                  {['React 18', 'TypeScript', 'Vite', 'Tailwind CSS', 'Zustand', 'React Router'].map((tech) => (
                    <span
                      key={tech}
                      className="px-3 py-1 rounded-full bg-accent/20 text-accent text-sm"
                    >
                      {tech}
                    </span>
                  ))}
                </div>
              </div>
            </div>
          </TabsContent>
        </Tabs>
      </Card>
    </div>
  );
}
