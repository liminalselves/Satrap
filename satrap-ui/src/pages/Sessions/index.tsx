import { useEffect, useState } from 'react';
import { useConfigStore } from '@/stores/useConfigStore';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Modal } from '@/components/ui/Modal';
import { Input } from '@/components/ui/Input';
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from '@/components/ui/Table';
import { toast } from '@/components/ui/Toast';
import { sessionApi } from '@/api/session';
import { Plus, Power, PowerOff, Trash2, Settings } from 'lucide-react';

export function Sessions() {
  const { sessionClasses, fetchSessionClasses } = useConfigStore();
  const [showRegisterModal, setShowRegisterModal] = useState(false);
  const [showParamsModal, setShowParamsModal] = useState(false);
  const [selectedClass, setSelectedClass] = useState<string | null>(null);
  const [paramsJson, setParamsJson] = useState('{}');
  const [registerForm, setRegisterForm] = useState({
    name: '',
    class_path: '',
    description: '',
  });

  useEffect(() => {
    fetchSessionClasses();
  }, [fetchSessionClasses]);

  const handleEnable = async (name: string) => {
    try {
      await sessionApi.enable(name);
      toast('success', `已启用 ${name}`);
      fetchSessionClasses();
    } catch {
      toast('error', '启用失败');
    }
  };

  const handleDisable = async (name: string) => {
    try {
      await sessionApi.disable(name);
      toast('success', `已禁用 ${name}`);
      fetchSessionClasses();
    } catch {
      toast('error', '禁用失败');
    }
  };

  const handleUnregister = async (name: string) => {
    if (!confirm(`确定要注销会话类 "${name}" 吗？`)) return;
    try {
      await sessionApi.unregister(name);
      toast('success', '已注销');
      fetchSessionClasses();
    } catch {
      toast('error', '注销失败');
    }
  };

  const handleEditParams = (name: string) => {
    const config = sessionClasses[name];
    setSelectedClass(name);
    setParamsJson(JSON.stringify(config?.params || {}, null, 2));
    setShowParamsModal(true);
  };

  const handleSaveParams = async () => {
    if (!selectedClass) return;
    try {
      const params = JSON.parse(paramsJson);
      await sessionApi.updateParams(selectedClass, params);
      toast('success', '参数已保存');
      setShowParamsModal(false);
      fetchSessionClasses();
    } catch (e) {
      toast('error', '保存失败: ' + (e instanceof Error ? e.message : '未知错误'));
    }
  };

  const handleRegister = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await sessionApi.register(registerForm);
      toast('success', '注册成功');
      setShowRegisterModal(false);
      setRegisterForm({ name: '', class_path: '', description: '' });
      fetchSessionClasses();
    } catch (e) {
      toast('error', '注册失败: ' + (e instanceof Error ? e.message : '未知错误'));
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-text-primary">会话管理</h1>
          <p className="text-text-secondary mt-1">管理会话类注册和配置</p>
        </div>
        <Button variant="primary" onClick={() => setShowRegisterModal(true)}>
          <Plus className="h-4 w-4 mr-2" />
          注册会话类
        </Button>
      </div>

      <Card>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>名称</TableHead>
              <TableHead>Class Path</TableHead>
              <TableHead>状态</TableHead>
              <TableHead>模型键</TableHead>
              <TableHead>操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {Object.entries(sessionClasses).map(([name, config]) => (
              <TableRow key={name}>
                <TableCell className="font-medium">{name}</TableCell>
                <TableCell className="font-mono text-sm text-text-secondary">
                  {config.class_path}
                </TableCell>
                <TableCell>
                  <Badge variant={config.enabled ? 'success' : 'default'}>
                    {config.enabled ? '启用' : '禁用'}
                  </Badge>
                </TableCell>
                <TableCell>{config.model_key || '-'}</TableCell>
                <TableCell>
                  <div className="flex items-center gap-2">
                    {config.enabled ? (
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => handleDisable(name)}
                        title="禁用"
                      >
                        <PowerOff className="h-4 w-4" />
                      </Button>
                    ) : (
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => handleEnable(name)}
                        title="启用"
                      >
                        <Power className="h-4 w-4" />
                      </Button>
                    )}
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => handleEditParams(name)}
                      title="编辑参数"
                    >
                      <Settings className="h-4 w-4" />
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => handleUnregister(name)}
                      title="注销"
                      className="text-error hover:text-error"
                    >
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </div>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>

        {Object.keys(sessionClasses).length === 0 && (
          <div className="text-center py-12 text-text-secondary">
            暂无已注册的会话类
          </div>
        )}
      </Card>

      {/* 注册模态框 */}
      <Modal
        open={showRegisterModal}
        onClose={() => setShowRegisterModal(false)}
        title="注册新会话类"
      >
        <form onSubmit={handleRegister} className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-text-secondary mb-1">
              名称
            </label>
            <Input
              value={registerForm.name}
              onChange={(e) => setRegisterForm({ ...registerForm, name: e.target.value })}
              required
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-text-secondary mb-1">
              Class Path
            </label>
            <Input
              value={registerForm.class_path}
              onChange={(e) => setRegisterForm({ ...registerForm, class_path: e.target.value })}
              placeholder="如: misskey_session.MisskeySession"
              required
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-text-secondary mb-1">
              描述（可选）
            </label>
            <Input
              value={registerForm.description}
              onChange={(e) => setRegisterForm({ ...registerForm, description: e.target.value })}
            />
          </div>
          <div className="flex gap-3 pt-4">
            <Button type="submit" variant="primary" className="flex-1">
              注册
            </Button>
            <Button type="button" variant="default" onClick={() => setShowRegisterModal(false)}>
              取消
            </Button>
          </div>
        </form>
      </Modal>

      {/* 参数编辑模态框 */}
      <Modal
        open={showParamsModal}
        onClose={() => setShowParamsModal(false)}
        title={`编辑参数: ${selectedClass}`}
        size="lg"
      >
        <div className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-text-secondary mb-1">
              参数 (JSON 格式)
            </label>
            <textarea
              value={paramsJson}
              onChange={(e) => setParamsJson(e.target.value)}
              className="glass-input w-full h-64 font-mono text-sm resize-none"
            />
          </div>
          <div className="flex gap-3">
            <Button variant="primary" onClick={handleSaveParams} className="flex-1">
              保存
            </Button>
            <Button variant="default" onClick={() => setShowParamsModal(false)}>
              取消
            </Button>
          </div>
        </div>
      </Modal>
    </div>
  );
}
