import { useState } from 'react';
import type { ImgConfig } from '@/api/types';
import { FormModal, type FormField } from '@/components/common/FormModal';
import { normalizeImgConfig, readImgParams, writeImgParams } from './imgModel';
import { ImgParamsFields } from './ImgParamsFields';

interface Props {
  name: string | null;
  initialConfig?: ImgConfig;
  onClose: () => void;
  onSave: (config: ImgConfig) => Promise<boolean>;
}

export function ImgConfigModal({ name, initialConfig, onClose, onSave }: Props) {
  const isNew = name === null;
  const [draft, setDraft] = useState<ImgConfig>(() => ({
    api_type: 'openai_images', timeout: 180, lock_api_key: true, allow_insecure_base_url: false,
    ...initialConfig, name: name ?? 'default',
    api_key: initialConfig?.api_key?.includes('*') ? '' : initialConfig?.api_key,
  }));
  const [params, setParams] = useState(() => readImgParams(initialConfig?.default_params));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const fields: FormField[] = [
    { key: 'name', label: '配置名称', required: true },
    { key: 'model', label: '模型 ID', required: true },
    { key: 'api_type', label: '请求协议', type: 'select', options: [
      { value: 'openai_images', label: 'OpenAI Images' },
      { value: 'seedream', label: 'Seedream' },
      { value: 'novelai', label: 'NovelAI' },
    ] },
    { key: 'base_url', label: '生成请求地址', required: true, placeholder: '填写完整端点, 如 https://api.openai.com/v1/images/generations' },
    { key: 'edit_base_url', label: '编辑请求地址', placeholder: '选填完整端点, 留空与生成请求使用相同地址' },
    { key: 'api_key', label: 'API Key', type: 'password', placeholder: isNew ? 'NovelAI 使用 Persistent API Token' : '留空保留已保存的密钥' },
    { key: 'timeout', label: '网络超时 (秒)', type: 'number', min: 0.1 },
    { key: 'drawing_params', label: '绘图默认参数', type: 'custom', render: () =>
      <ImgParamsFields apiType={draft.api_type} value={params} onChange={(next) => { setError(''); setParams(next); }} /> },
    { key: 'lock_api_key', label: '锁定密钥属性', type: 'checkbox', placeholder: '客户端公开属性不显示密钥' },
    { key: 'allow_insecure_base_url', label: '允许 HTTP 地址', type: 'checkbox', placeholder: '允许使用非本机 HTTP 接口' },
  ];

  const save = async () => {
    if (saving) return;
    setError('');
    try {
      const config = normalizeImgConfig(draft, writeImgParams(params, draft.api_type), isNew);
      setSaving(true);
      if (await onSave(config)) onClose();
      else setError('保存失败, 请检查配置或稍后重试');
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '保存失败');
    } finally {
      setSaving(false);
    }
  };

  return <FormModal open title={isNew ? '新增图片模型' : `编辑图片模型: ${name}`} size="xl"
    onClose={onClose} fields={fields} values={{ ...draft }}
    onChange={(key, value) => {
      setError('');
      setDraft((previous) => ({ ...previous, [key]: value }));
    }}
    onSubmit={() => void save()} loading={saving} submitText={isNew ? '创建' : '保存修改'}
    statusContent={<div className="space-y-2 text-sm">
      <p className="text-text-tertiary">参考图片在实际绘图时提供，模型配置只保存默认参数。</p>
      {error && <p role="alert" className="text-error">{error}</p>}
    </div>} />;
}
