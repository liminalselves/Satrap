import { useEffect, useState } from 'react';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Modal } from '@/components/ui/Modal';
import { Input } from '@/components/ui/Input';
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from '@/components/ui/Table';
import { toast } from '@/components/ui/Toast';
import { userApi } from '@/api/user';
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

  const fetchUsers = async () => {
    setLoading(true);
    try {
      const data = await userApi.list(500);
      setUsers(data.users);
    } catch {
      toast('error', '获取用户列表失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchUsers();
  }, []);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await userApi.create(formData.user_id, formData.platform, formData.nickname);
      toast('success', '用户已创建');
      setShowCreateModal(false);
      setFormData({ user_id: '', platform: '', nickname: '' });
      fetchUsers();
    } catch (e) {
      toast('error', '创建失败: ' + (e instanceof Error ? e.message : '未知错误'));
    }
  };

  const handleUpdate = async (e: React.FormEvent) => {
    e.preventDefault();
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
  };

  const handleDelete = async (userId: string) => {
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
  };

  const handleBind = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedUser) return;
    try {
      await userApi.bindSession(selectedUser.user_id, bindSessionId);
      toast('success', '已绑定');
      setShowBindModal(false);
      setBindSessionId('');
      fetchUsers();
      // 更新选中的用户信息
      const updated = await userApi.get(selectedUser.user_id);
      if (updated.user) setSelectedUser(updated.user);
    } catch {
      toast('error', '绑定失败');
    }
  };

  const handleUnbind = async (sessionId: string) => {
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
  };

  const openEdit = (user: UserInfo) => {
    setSelectedUser(user);
    setFormData({
      user_id: user.user_id,
      platform: user.user_platform || '',
      nickname: user.user_nickname || '',
    });
    setShowEditModal(true);
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-text-primary">用户管理</h1>
          <p className="text-text-secondary mt-1">管理系统用户和会话绑定</p>
        </div>
        <Button variant="primary" onClick={() => setShowCreateModal(true)}>
          <Plus className="h-4 w-4 mr-2" />
          新建用户
        </Button>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* 用户列表 */}
        <div className="lg:col-span-2">
          <Card>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>用户 ID</TableHead>
                  <TableHead>平台</TableHead>
                  <TableHead>昵称</TableHead>
                  <TableHead>会话数</TableHead>
                  <TableHead>操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {users.map((user) => (
                  <TableRow
                    key={user.user_id}
                    className={selectedUser?.user_id === user.user_id ? 'bg-accent/10' : ''}
                    onClick={() => setSelectedUser(user)}
                  >
                    <TableCell className="font-mono">{user.user_id}</TableCell>
                    <TableCell>{user.user_platform || '-'}</TableCell>
                    <TableCell>{user.user_nickname || '-'}</TableCell>
                    <TableCell>
                      <Badge variant="info">{user.user_session?.length || 0}</Badge>
                    </TableCell>
                    <TableCell>
                      <div className="flex items-center gap-2">
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={(e) => {
                            e.stopPropagation();
                            openEdit(user);
                          }}
                        >
                          <Edit2 className="h-4 w-4" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={(e) => {
                            e.stopPropagation();
                            handleDelete(user.user_id);
                          }}
                          className="text-error"
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>

            {users.length === 0 && !loading && (
              <div className="text-center py-12 text-text-secondary">
                暂无用户
              </div>
            )}
          </Card>
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
                          className="flex items-center justify-between p-2 rounded bg-bg-glass"
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
      <Modal
        open={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        title="新建用户"
      >
        <form onSubmit={handleCreate} className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-text-secondary mb-1">
              用户 ID *
            </label>
            <Input
              value={formData.user_id}
              onChange={(e) => setFormData({ ...formData, user_id: e.target.value })}
              placeholder="如: misskey:xxxxxxxx"
              required
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-text-secondary mb-1">
              平台
            </label>
            <Input
              value={formData.platform}
              onChange={(e) => setFormData({ ...formData, platform: e.target.value })}
              placeholder="如: misskey / onebot"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-text-secondary mb-1">
              昵称
            </label>
            <Input
              value={formData.nickname}
              onChange={(e) => setFormData({ ...formData, nickname: e.target.value })}
            />
          </div>
          <div className="flex gap-3 pt-4">
            <Button type="submit" variant="primary" className="flex-1">
              创建
            </Button>
            <Button type="button" variant="default" onClick={() => setShowCreateModal(false)}>
              取消
            </Button>
          </div>
        </form>
      </Modal>

      {/* 编辑用户模态框 */}
      <Modal
        open={showEditModal}
        onClose={() => setShowEditModal(false)}
        title="编辑用户"
      >
        <form onSubmit={handleUpdate} className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-text-secondary mb-1">
              用户 ID
            </label>
            <Input value={formData.user_id} disabled />
          </div>
          <div>
            <label className="block text-sm font-medium text-text-secondary mb-1">
              平台
            </label>
            <Input
              value={formData.platform}
              onChange={(e) => setFormData({ ...formData, platform: e.target.value })}
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-text-secondary mb-1">
              昵称
            </label>
            <Input
              value={formData.nickname}
              onChange={(e) => setFormData({ ...formData, nickname: e.target.value })}
            />
          </div>
          <div className="flex gap-3 pt-4">
            <Button type="submit" variant="primary" className="flex-1">
              保存
            </Button>
            <Button type="button" variant="default" onClick={() => setShowEditModal(false)}>
              取消
            </Button>
          </div>
        </form>
      </Modal>

      {/* 绑定会话模态框 */}
      <Modal
        open={showBindModal}
        onClose={() => setShowBindModal(false)}
        title="绑定会话"
      >
        <form onSubmit={handleBind} className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-text-secondary mb-1">
              Session ID
            </label>
            <Input
              value={bindSessionId}
              onChange={(e) => setBindSessionId(e.target.value)}
              placeholder="如: sr7dws"
              required
            />
          </div>
          <div className="flex gap-3 pt-4">
            <Button type="submit" variant="primary" className="flex-1">
              绑定
            </Button>
            <Button type="button" variant="default" onClick={() => setShowBindModal(false)}>
              取消
            </Button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
