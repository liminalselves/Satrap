import { useState } from 'react';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/Tabs';
import { toast } from '@/components/ui/Toast';
import { useBackendStore } from '@/stores/useBackendStore';
import { Save, RotateCcw, Power } from 'lucide-react';

export function Settings() {
  const { health, reloadConfig, shutdown } = useBackendStore();
  const [config, setConfig] = useState({
    api_host: '127.0.0.1',
    api_port: 19870,
    default_session_type: 'default',
    max_sessions: 100,
    idle_timeout: 3600,
    llm_timeout: 60,
    rate_limit: 10,
    rate_burst: 20,
  });

  const handleSave = async () => {
    // TODO: 调用 API 保存配置
    toast('success', '配置已保存');
  };

  const handleReload = async () => {
    const ok = await reloadConfig();
    if (ok) {
      toast('success', '配置已重载');
    } else {
      toast('error', '重载失败');
    }
  };

  const handleShutdown = async () => {
    if (!confirm('确定要停止后端吗？')) return;
    const ok = await shutdown();
    if (ok) {
      toast('success', '后端已停止');
    } else {
      toast('error', '停止失败');
    }
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-text-primary">系统设置</h1>
        <p className="text-text-secondary mt-1">配置系统参数和后端控制</p>
      </div>

      {/* 后端控制 */}
      <Card>
        <h3 className="text-lg font-semibold text-text-primary mb-4">后端控制</h3>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-4">
            <div className={`w-3 h-3 rounded-full ${health?.running ? 'bg-success' : 'bg-error'}`} />
            <span className="text-text-primary">
              {health?.running ? '后端运行中' : '后端未运行'}
            </span>
            {health?.running && (
              <span className="text-text-secondary text-sm">
                {`http://${config.api_host}:${config.api_port}`}
              </span>
            )}
          </div>
          <div className="flex gap-2">
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
      </Card>

      {/* 配置编辑 */}
      <Card>
        <Tabs defaultValue="general">
          <TabsList>
            <TabsTrigger value="general">常用配置</TabsTrigger>
            <TabsTrigger value="raw">原始配置</TabsTrigger>
            <TabsTrigger value="about">关于</TabsTrigger>
          </TabsList>

          <TabsContent value="general">
            <div className="grid grid-cols-2 gap-6">
              <div>
                <label className="block text-sm font-medium text-text-secondary mb-1">
                  API Host
                </label>
                <Input
                  value={config.api_host}
                  onChange={(e) => setConfig({ ...config, api_host: e.target.value })}
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-text-secondary mb-1">
                  API Port
                </label>
                <Input
                  type="number"
                  value={config.api_port}
                  onChange={(e) => setConfig({ ...config, api_port: Number(e.target.value) })}
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-text-secondary mb-1">
                  默认会话类型
                </label>
                <Input
                  value={config.default_session_type}
                  onChange={(e) => setConfig({ ...config, default_session_type: e.target.value })}
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-text-secondary mb-1">
                  最大会话数
                </label>
                <Input
                  type="number"
                  value={config.max_sessions}
                  onChange={(e) => setConfig({ ...config, max_sessions: Number(e.target.value) })}
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-text-secondary mb-1">
                  会话闲置超时（秒）
                </label>
                <Input
                  type="number"
                  value={config.idle_timeout}
                  onChange={(e) => setConfig({ ...config, idle_timeout: Number(e.target.value) })}
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-text-secondary mb-1">
                  LLM 超时（秒）
                </label>
                <Input
                  type="number"
                  value={config.llm_timeout}
                  onChange={(e) => setConfig({ ...config, llm_timeout: Number(e.target.value) })}
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-text-secondary mb-1">
                  限流速率
                </label>
                <Input
                  type="number"
                  value={config.rate_limit}
                  onChange={(e) => setConfig({ ...config, rate_limit: Number(e.target.value) })}
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-text-secondary mb-1">
                  限流突发数
                </label>
                <Input
                  type="number"
                  value={config.rate_burst}
                  onChange={(e) => setConfig({ ...config, rate_burst: Number(e.target.value) })}
                />
              </div>
            </div>

            <div className="flex gap-3 mt-6 pt-6 border-t border-border-glass">
              <Button variant="primary" onClick={handleSave}>
                <Save className="h-4 w-4 mr-2" />
                保存配置
              </Button>
            </div>
          </TabsContent>

          <TabsContent value="raw">
            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-text-secondary mb-1">
                  完整配置 (YAML/JSON)
                </label>
                <textarea
                  className="glass-input w-full h-96 font-mono text-sm resize-none"
                  placeholder="# 配置文件内容"
                  defaultValue={`# Satrap 配置
api:
  host: ${config.api_host}
  port: ${config.api_port}

default_session_type: ${config.default_session_type}
max_sessions: ${config.max_sessions}
idle_timeout: ${config.idle_timeout}
llm_timeout: ${config.llm_timeout}
rate_limit: ${config.rate_limit}
rate_burst: ${config.rate_burst}

platforms: []
`}
                />
              </div>
              <div className="flex gap-3">
                <Button variant="primary" onClick={handleSave}>
                  <Save className="h-4 w-4 mr-2" />
                  保存原始配置
                </Button>
              </div>
            </div>
          </TabsContent>

          <TabsContent value="about">
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div className="p-4 rounded-glass-sm bg-bg-glass">
                  <p className="text-sm text-text-tertiary">前端版本</p>
                  <p className="text-lg font-semibold text-text-primary">1.0.0</p>
                </div>
                <div className="p-4 rounded-glass-sm bg-bg-glass">
                  <p className="text-sm text-text-tertiary">后端 API</p>
                  <p className="text-lg font-semibold text-text-primary">
                    {health?.running ? '已连接' : '未连接'}
                  </p>
                </div>
              </div>
              <div className="p-4 rounded-glass-sm bg-bg-glass">
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
