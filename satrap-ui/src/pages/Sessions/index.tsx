import { useEffect, useState, useCallback, useMemo } from 'react';
import { useConfigStore } from '@/stores/useConfigStore';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { toast } from '@/components/ui/Toast';
import { sessionApi } from '@/api/session';
import { PageHeader, DataTable, FormModal, ActionButtons, Column, FormField } from '@/components/common';
import { Plus, Power, PowerOff, Trash2, Settings } from 'lucide-react';

interface SessionClassConfig {
  class_path: string;
  enabled: boolean;
  model_key?: string;
  params?: Record<string, unknown>;
}

interface SessionClassItem {
  name: string;
  config: SessionClassConfig;
}

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

  // 转换为表格数据
  const tableData: SessionClassItem[] = useMemo(() => 
    Object.entries(sessionClasses).map(([name, config]) => ({ name, config })),
    [sessionClasses]
  );

  // 操作处理
  const handleEnable = useCallback(async (name: string) => {
    try {
      await sessionApi.enable(name);
      toast('success', `已启用 ${name}`);
      fetchSessionClasses();
    } catch {
      toast('error', '启用失败');
    }
  }, [fetchSessionClasses]);

  const handleDisable = useCallback(async (name: string) => {
    try {
      await sessionApi.disable(name);
      toast('success', `已禁用 ${name}`);
      fetchSessionClasses();
    } catch {
      toast('error', '禁用失败');
    }
  }, [fetchSessionClasses]);

  const handleUnregister = useCallback(async (name: string) => {
    if (!confirm(`确定要注销会话类 "${name}" 吗？`)) return;
    try {
      await sessionApi.unregister(name);
      toast('success', '已注销');
      fetchSessionClasses();
    } catch {
      toast('error', '注销失败');
    }
  }, [fetchSessionClasses]);

  const handleEditParams = useCallback((name: string) => {
    const config = sessionClasses[name];
    setSelectedClass(name);
    setParamsJson(JSON.stringify(config?.params || {}, null, 2));
    setShowParamsModal(true);
  }, [sessionClasses]);

  const handleSaveParams = useCallback(async () => {
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
  }, [selectedClass, paramsJson, fetchSessionClasses]);

  const handleRegister = useCallback(async () => {
    try {
      await sessionApi.register(registerForm);
      toast('success', '注册成功');
      setShowRegisterModal(false);
      setRegisterForm({ name: '', class_path: '', description: '' });
      fetchSessionClasses();
    } catch (e) {
      toast('error', '注册失败: ' + (e instanceof Error ? e.message : '未知错误'));
    }
  }, [registerForm, fetchSessionClasses]);

  // 表格列定义
  const columns: Column<SessionClassItem>[] = useMemo(() => [
    {
      key: 'name',
      title: '名称',
      render: (item) => <span className="font-medium">{item.name}</span>,
    },
    {
      key: 'class_path',
      title: 'Class Path',
      render: (item) => (
        <span className="font-mono text-sm text-text-secondary">{item.config.class_path}</span>
      ),
    },
    {
      key: 'enabled',
      title: '状态',
      render: (item) => (
        <Badge variant={item.config.enabled ? 'success' : 'default'}>
          {item.config.enabled ? '启用' : '禁用'}
        </Badge>
      ),
    },
    {
      key: 'model_key',
      title: '模型键',
      render: (item) => item.config.model_key || '-',
    },
    {
      key: 'actions',
      title: '操作',
      render: (item) => (
        <ActionButtons
          actions={[
            item.config.enabled
              ? {
                  key: 'disable',
                  icon: <PowerOff className="h-4 w-4" />,
                  onClick: () => handleDisable(item.name),
                  title: '禁用',
                }
              : {
                  key: 'enable',
                  icon: <Power className="h-4 w-4" />,
                  onClick: () => handleEnable(item.name),
                  title: '启用',
                },
            {
              key: 'edit',
              icon: <Settings className="h-4 w-4" />,
              onClick: () => handleEditParams(item.name),
              title: '编辑参数',
            },
            {
              key: 'delete',
              icon: <Trash2 className="h-4 w-4" />,
              onClick: () => handleUnregister(item.name),
              title: '注销',
              className: 'text-error hover:text-error',
            },
          ]}
        />
      ),
    },
  ], [handleEnable, handleDisable, handleEditParams, handleUnregister]);

  // 注册表单字段
  const registerFields: FormField[] = useMemo(() => [
    { key: 'name', label: '名称', required: true },
    { key: 'class_path', label: 'Class Path', placeholder: '如: misskey_session.MisskeySession', required: true },
    { key: 'description', label: '描述（可选）' },
  ], []);

  // 参数表单字段
  const paramsFields: FormField[] = useMemo(() => [
    { key: 'params', label: '参数 (JSON 格式)', type: 'textarea', rows: 16 },
  ], []);

  return (
    <div className="space-y-6">
      <PageHeader
        title="会话管理"
        description="管理会话类注册和配置"
        actions={
          <Button variant="primary" onClick={() => setShowRegisterModal(true)}>
            <Plus className="h-4 w-4 mr-2" />
            注册会话类
          </Button>
        }
      />

      <DataTable
        columns={columns}
        data={tableData}
        keyExtractor={(item) => item.name}
        emptyMessage="暂无已注册的会话类"
      />

      {/* 注册模态框 */}
      <FormModal
        open={showRegisterModal}
        onClose={() => setShowRegisterModal(false)}
        title="注册新会话类"
        fields={registerFields}
        values={registerForm}
        onChange={(key, value) => setRegisterForm({ ...registerForm, [key]: value })}
        onSubmit={handleRegister}
        submitText="注册"
      />

      {/* 参数编辑模态框 */}
      <FormModal
        open={showParamsModal}
        onClose={() => setShowParamsModal(false)}
        title={`编辑参数: ${selectedClass}`}
        fields={paramsFields}
        values={{ params: paramsJson }}
        onChange={(_, value) => setParamsJson(value as string)}
        onSubmit={handleSaveParams}
        submitText="保存"
        size="lg"
      />
    </div>
  );
}
