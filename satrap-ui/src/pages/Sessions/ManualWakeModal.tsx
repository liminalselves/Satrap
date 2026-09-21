import { useEffect, useRef, useState } from 'react';
import { FormModal } from '@/components/common';
import { toast } from '@/components/ui/Toast';
import { backendApi } from '@/api/backend';
import { controlApi } from '@/api/control';
import type { PlatformConfig } from '@/api/types';

export function ManualWakeModal({ onClose }: { onClose: () => void }) {
  const [platforms, setPlatforms] = useState<PlatformConfig[]>([]);
  const [saving, setSaving] = useState(false);
  const [form, setForm] = useState({ adapter_id: '', group_id: '', user_id: '', prompt: '', message_id: '' });
  const request = useRef({ content: '', id: '' });
  useEffect(() => {
    let cancelled = false;
    controlApi.listPlatforms().then((result) => {
      if (!result.ok) throw new Error(result.error || '读取平台失败');
      if (!cancelled) setPlatforms((result.platforms || []).filter((item) => item.enable !== false && ['onebot', 'aiocqhttp'].includes(item.type)));
    }).catch((error) => toast('error', error.message));
    return () => { cancelled = true; };
  }, []);

  const submit = async () => {
    if (!form.adapter_id || !/^[1-9]\d*$/.test(form.group_id) || !/^[1-9]\d*$/.test(form.user_id)) {
      toast('error', '请选择平台并填写有效的群号和会话成员 ID');
      return;
    }
    if (form.prompt.trim() && form.message_id.trim()) {
      toast('error', '唤醒正文与消息 ID 只能选择一项');
      return;
    }
    const content = JSON.stringify(form);
    if (request.current.content !== content) request.current = { content, id: crypto.randomUUID() };
    setSaving(true);
    try {
      const result = await backendApi.wakePlatform({ ...form, message_id: form.message_id.trim(), request_id: request.current.id });
      if (result.status === 'accepted') toast('success', '唤醒请求已入队, 回复将发送到目标群');
      else if (result.status === 'already_pending') toast('info', '此请求已受理, 未重复提交');
      else if (result.status === 'no_pending') { toast('info', '此群与成员范围内没有待处理正文'); return; }
      else throw new Error(result.reason || '请求被拒绝');
      onClose();
    } catch (error) {
      toast('error', '唤醒失败, 输入已保留: ' + (error instanceof Error ? error.message : '请求失败'));
    } finally {
      setSaving(false);
    }
  };

  return <FormModal open onClose={onClose} title="手动唤醒群聊" size="lg" loading={saving} submitText="提交唤醒"
    values={form} onChange={(key, value) => setForm((current) => ({ ...current, [key]: String(value) }))} onSubmit={submit}
    fields={[
      { key: 'adapter_id', label: '平台实例', type: 'select', options: [{ value: '', label: '请选择 OneBot 平台' }, ...platforms.map((item) => ({ value: item.id, label: item.id }))] },
      { key: 'group_id', label: '目标群号', type: 'text' },
      { key: 'user_id', label: '会话成员 ID（用于选择上下文，不代表操作者）', type: 'text' },
      { key: 'prompt', label: '唤醒正文（可选）', type: 'textarea', rows: 4, placeholder: '正文和消息 ID 均为空时, 处理此范围待处理正文' },
      { key: 'message_id', label: '指定消息 ID（可选，须属于目标群和成员）', type: 'text' },
    ]} />;
}
