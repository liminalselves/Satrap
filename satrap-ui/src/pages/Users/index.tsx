import { useEffect, useState, useCallback, useMemo } from 'react';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { toast } from '@/components/ui/Toast';
import { userApi } from '@/api/user';
import { PageHeader, DataTable, FormModal, ActionButtons, Column, FormField } from '@/components/common';
import { Plus, Edit2, Trash2, Link, Unlink } from 'lucide-react';
import type { UserInfo } from '@/api/types';

export function Users() {
  const [users, setUsers] = useState<UserInfo[]>([]);
  const [loading, setLoading] = useState(false);
  const [selectedUser, setSelectedUser] = useState<UserInfo | null>(null);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showEditModal, setShowEditModal] = useState(false);
  const [showBindModal, setShowBindModal] = useState(false);
  const [formData, setFormData] = useState({
    user_id: '',
    platform: '',
    nickname: '',
  });
  const [bindSessionId, setBindSessionId] = useState('');

  const fetchUsers = useCallback(async () => {
    setLoading(true);
    try {
      const data = await userApi.list(500);
      setUsers(data.users);
    } catch {
      toast('error', '获取用户列表失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchUsers();
  }, [fetchUsers]);

  const handleCreate = useCallback(async () => {
    try {
      await userApi.create(formData.user_id, formData.platform, formData.nickname);
      toast('success', '用户已创建');
      setShowCreateModal(false);
      setFormData({ user_id: '', platform: '', nickname: '' });
      fetchUsers();
    } catch (e) {
      toast('error', '创建失败: ' + (e instanceof Error ? e.message : '未知错误'));
    }
  }, [formData, fetchUsers]);

  const handleUpdate = useCallback(async () => {
    if (!selectedUser) return;
    try {
      await userApi.update(selectedUser.user_id, {
        nickname: formData.nickname,
        platform: formData.platform,
      });
      toast('success', '已保存');
      setShowEditModal(false);
      fetchUsers();
    } catch {
      toast('error', '保存失败');
    }
  }, [selectedUser, formData, fetchUsers]);

  const handleDelete = useCallback(async (userId: string) => {
    if (!confirm(`确定要删除用户 "${userId}" 吗？`)) return;
    try {
      await userApi.delete(userId);
      toast('success', '已删除');
      if (selectedUser?.user_id === userId) {
        setSelectedUser(null);
      }
      fetchUsers();
    } catch {
      toast('error', '删除失败');
    }
  }, [selectedUser, fetchUsers]);

  const handleBind = useCallback(async () => {
    if (!selectedUser) return;
    try {
      await userApi.bindSession(selectedUser.user_id, bindSessionId);
      toast('success', '已绑定');
      setShowBindModal(false);
      setBindSessionId('');
      fetchUsers();
      const updated = await userApi.get(selectedUser.user_id);
      if (updated.user) setSelectedUser(updated.user);
    } catch {
      toast('error', '绑定失败');
    }
  }, [selectedUser, bindSessionId, fetchUsers]);

  const handleUnbind = useCallback(async (sessionId: string) => {
    if (!selectedUser) return;
    try {
      await userApi.unbindSession(selectedUser.user_id, sessionId);
      toast('success', '已解绑');
      fetchUsers();
      const updated = await userApi.get(selectedUser.user_id);
      if (updated.user) setSelectedUser(updated.user);
    } catch {
      toast('error', '解绑失败');
    }
  }, [selectedUser, fetchUsers]);

  const openEdit = useCallback((user: UserInfo) => {
    setSelectedUser(user);
    setFormData({
      user_id: user.user_id,
      platform: user.user_platform || '',
      nickname: user.user_nickname || '',
    });
    setShowEditModal(true);
  }, []);

  const handleFieldChange = useCallback((key: string, value: unknown) => {
    setFormData((prev) => ({ ...prev, [key]: value }));
  }, []);

  // 表格列定义
  const columns: Column<UserInfo>[] = useMemo(() => [
    {
      key: 'user_id',
      title: '用户 ID',
      render: (user) => <span className="font-mono">{user.user_id}</span>,
    },
    {
      key: 'user_platform',
      title: '平台',
      render: (user) => user.user_platform || '-',
    },
    {
      key: 'user_nickname',
      title: '昵称',
      render: (user) => user.user_nickname || '-',
    },
    {
      key: 'sessions',
      title: '会话数',
      render: (user) => <Badge variant="info">{user.user_session?.length || 0}</Badge>,
    },
    {
      key: 'actions',
      title: '操作',
      render: (user) => (
        <ActionButtons
          actions={[
            {
              key: 'edit',
              icon: <Edit2 className="h-4 w-4" />,
              onClick: () => openEdit(user),
              title: '编辑',
            },
            {
              key: 'delete',
              icon: <Trash2 className="h-4 w-4" />,
              onClick: () => handleDelete(user.user_id),
              title: '删除',
              className: 'text-error',
            },
          ]}
        />
      ),
    },
  ], [openEdit, handleDelete]);

  // 表单字段
  const createFields: FormField[] = useMemo(() => [
    { key: 'user_id', label: '用户 ID', placeholder: '如: misskey:xxxxxxxx', required: true },
    { key: 'platform', label: '平台', placeholder: '如: misskey / onebot' },
    { key: 'nickname', label: '昵称' },
  ], []);

  const editFields: FormField[] = useMemo(() => [
    { key: 'user_id', label: '用户 ID', disabled: true },
    { key: 'platform', label: '平台' },
    { key: 'nickname', label: '昵称' },
  ], []);

  const bindFields: FormField[] = useMemo(() => [
    { key: 'session_id', label: 'Session ID', placeholder: '如: sr7dws', required: true },
  ], []);

  return (
    <div className="space-y-6">
      <PageHeader
        title="用户管理"
        description="管理系统用户和会话绑定"
        actions={
          <Button variant="primary" onClick={() => setShowCreateModal(true)}>
            <Plus className="h-4 w-4 mr-2" />
            新建用户
          </Button>
        }
      />

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* 用户列表 */}
        <div className="lg:col-span-2">
          <DataTable
            columns={columns}
            data={users}
            keyExtractor={(user) => user.user_id}
            emptyMessage={loading ? '加载中...' : '暂无用户'}
          />
        </div>

        {/* 用户详情 */}
        <div className="lg:col-span-1">
          <Card className="sticky top-6">
            <h3 className="text-lg font-semibold text-text-primary mb-4">用户详情</h3>
            {selectedUser ? (
              <div className="space-y-4">
                <div>
                  <label className="text-sm text-text-tertiary">用户 ID</label>
                  <p className="font-mono text-text-primary">{selectedUser.user_id}</p>
                </div>
                <div>
                  <label className="text-sm text-text-tertiary">平台</label>
                  <p className="text-text-primary">{selectedUser.user_platform || '-'}</p>
                </div>
                <div>
                  <label className="text-sm text-text-tertiary">昵称</label>
                  <p className="text-text-primary">{selectedUser.user_nickname || '-'}</p>
                </div>

                <div className="border-t border-border-glass pt-4">
                  <div className="flex items-center justify-between mb-3">
                    <label className="text-sm text-text-tertiary">绑定的会话</label>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setShowBindModal(true)}
                    >
                      <Link className="h-4 w-4 mr-1" />
                      绑定
                    </Button>
                  </div>
                  {selectedUser.user_session && selectedUser.user_session.length > 0 ? (
                    <div className="space-y-2">
                      {selectedUser.user_session.map((sid) => (
                        <div
                          key={sid}
                          className="flex items-center justify-between p-2 rounded-sm bg-glass"
                        >
                          <code className="text-sm text-text-primary">{sid}</code>
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => handleUnbind(sid)}
                            className="text-error"
                          >
                            <Unlink className="h-3 w-3" />
                          </Button>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="text-text-tertiary text-sm">未绑定任何会话</p>
                  )}
                </div>
              </div>
            ) : (
              <p className="text-text-tertiary">点击左侧用户查看详情</p>
            )}
          </Card>
        </div>
      </div>

      {/* 创建用户模态框 */}
      <FormModal
        open={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        title="新建用户"
        fields={createFields}
        values={formData}
        onChange={handleFieldChange}
        onSubmit={handleCreate}
        submitText="创建"
      />

      {/* 编辑用户模态框 */}
      <FormModal
        open={showEditModal}
        onClose={() => setShowEditModal(false)}
        title="编辑用户"
        fields={editFields}
        values={formData}
        onChange={handleFieldChange}
        onSubmit={handleUpdate}
        submitText="保存"
      />

      {/* 绑定会话模态框 */}
      <FormModal
        open={showBindModal}
        onClose={() => setShowBindModal(false)}
        title="绑定会话"
        fields={bindFields}
        values={{ session_id: bindSessionId }}
        onChange={(_, value) => setBindSessionId(value as string)}
        onSubmit={handleBind}
        submitText="绑定"
      />
    </div>
  );
}
