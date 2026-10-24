import { useMemo, useState } from 'react';
import type { LLMConfig } from '@/api/types';
import { DEFAULT_THINKING_LEVELS, THINKING_FIELD_OPTIONS, THINKING_LEVEL_OPTIONS } from '@/utils/constants';
import { FormModal, type FormField } from './FormModal';

interface LLMConfigModalProps {
  modelName: string;
  initialConfig?: LLMConfig;
  defaultName?: string;
  allowRename?: boolean;
  onClose: () => void;
  onSave: (config: LLMConfig) => Promise<boolean>;
}

const FIELDS: FormField[] = [
  { key: 'name', label: '配置名称', required: true },
  { key: 'model', label: '模型 ID', placeholder: '如: gpt-4o' },
  { key: 'base_url', label: 'Base URL', placeholder: '如: https://api.openai.com/v1' },
  { key: 'api_key', label: 'API Key', type: 'password' },
  { key: 'temperature', label: '温度', type: 'number', min: 0, max: 2, step: 0.1 },
  { key: 'top_p', label: 'Top P', type: 'number', min: 0, max: 1, step: 0.05 },
  { key: 'max_tokens', label: 'Max Tokens', type: 'number', min: 1, step: 1 },
  { key: 'network_timeout', label: '网络超时 (秒)', type: 'number', min: 0.1, placeholder: '默认 60 秒, 限制网络读写等待, 不限制整轮会话' },
  { key: 'context_window', label: '上下文窗口', type: 'number', min: 1, step: 1, placeholder: '128000' },
  { key: 'history_ratio', label: '历史比例', type: 'number', min: 0, max: 1, step: 0.05, placeholder: '0.7' },
  { key: 'context_strategy', label: '上下文处理策略', type: 'select', options: [
    { value: 'sliding', label: '滑动窗口' },
    { value: 'mid_truncate', label: '中间截取' },
    { value: 'summarize', label: '总结压缩' },
  ] },
  { key: 'context_threshold', label: '触发比例', type: 'number', min: 0, max: 1, step: 0.05 },
  { key: 'truncation_floor', label: '压缩目标比例', type: 'number', min: 0, max: 1, step: 0.05 },
  { key: 'summary_keep_recent_turns', label: '保留最近轮数', type: 'number', min: 0, step: 1 },
  { key: 'thinking_field_name', label: '思考字段名', placeholder: 'reasoning_content' },
  { key: 'thinking_fields', label: '思考请求字段', type: 'checkbox-group', options: [...THINKING_FIELD_OPTIONS] },
  { key: 'thinking_levels', label: '可用思考强度', type: 'checkbox-group',
    options: THINKING_LEVEL_OPTIONS.map((option) => ({ ...option, label: `${option.value} ${option.label}` })) },
  { key: 'supports_visual_input', label: '支持图片输入', type: 'checkbox', placeholder: '允许接收静态图片和抽帧画面' },
  { key: 'supports_native_video_input', label: '支持原生视频输入', type: 'checkbox', placeholder: '声明所接 API 支持 video_url, 开启后直接传视频' },
  { key: 'video_frame_interval', label: '抽帧间隔 (秒)', type: 'custom', render: (value, onChange) => (
    <div className="flex items-center gap-3">
      <input type="range" min={0.5} max={5} step={0.1} value={typeof value === 'number' ? value : 2}
        aria-label="抽帧间隔滑块" className="flex-1 accent-accent" onChange={(event) => onChange(Number(event.target.value))} />
      <input type="number" min={0.5} max={5} step="any" value={typeof value === 'number' ? value : ''}
        aria-label="抽帧间隔秒数" className="glass-input w-24" onChange={(event) => onChange(event.target.value === '' ? undefined : Number(event.target.value))} />
    </div>
  ) },
  { key: 'omit_none_thinking_fields', label: '关闭思考兼容', type: 'checkbox', placeholder: '关闭思考时不发送值为 none 的字段' },
];

export function LLMConfigModal({ modelName, initialConfig, defaultName = '', allowRename = false, onClose, onSave }: LLMConfigModalProps) {
  const isNew = modelName === '';
  const [draft, setDraft] = useState<LLMConfig>(() => ({
    context_strategy: 'sliding', context_threshold: 0.8, truncation_floor: 0.4, summary_keep_recent_turns: 6,
    thinking_fields: [], thinking_levels: [...DEFAULT_THINKING_LEVELS], omit_none_thinking_fields: false,
    supports_visual_input: false, supports_native_video_input: false, video_frame_interval: 2.0, ...initialConfig,
    name: isNew ? defaultName : modelName,
    api_key: initialConfig?.api_key?.includes('*') ? '' : initialConfig?.api_key,
  }));   // 只在打开时建立编辑副本, 后台配置刷新不覆盖尚未保存的输入
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const fields = useMemo(() => FIELDS.filter((field) => (
    (field.key !== 'summary_keep_recent_turns' || draft.context_strategy === 'summarize')
    && (field.key !== 'video_frame_interval' || (draft.supports_visual_input && !draft.supports_native_video_input))
    && (field.key !== 'thinking_levels' || draft.thinking_fields?.some((value) => ['thinking_level', 'reasoning_effort'].includes(value)))
  )).map((field) => ({ ...field,
    ...(field.key === 'name' ? { disabled: !isNew && !allowRename } : {}),
    ...(field.key === 'api_key' ? { placeholder: isNew ? 'sk-...' : '留空表示不修改' } : {}),
  })), [draft.context_strategy, draft.thinking_fields, draft.supports_visual_input, draft.supports_native_video_input, isNew, allowRename]);

  const windowTokens = draft.context_window ?? 128000;
  const ratio = draft.history_ratio ?? 0.7;
  const threshold = draft.context_threshold;
  const floor = draft.truncation_floor;
  const recent = draft.summary_keep_recent_turns;
  const historyBudget = Math.floor(windowTokens * ratio);
  const triggerTokens = Math.floor(historyBudget * (threshold ?? 0));
  const floorTokens = Math.floor(historyBudget * (floor ?? 0));

  const handleSave = async () => {
    if (saving) return;
    if (!draft.name.trim()) return setError('请输入配置名称');
    if (!Number.isInteger(windowTokens) || windowTokens <= 0) return setError('上下文窗口必须是大于 0 的整数');
    if (!Number.isFinite(ratio) || ratio <= 0 || ratio > 1) return setError('历史比例必须大于 0 且不超过 1');
    if (threshold === undefined || floor === undefined || !Number.isFinite(threshold) || !Number.isFinite(floor)
      || floor <= 0 || floor >= threshold || threshold > 1) return setError('上下文比例必须满足 0 < 压缩目标比例 < 触发比例 <= 1');
    if (floorTokens <= 0 || floorTokens >= triggerTokens) return setError('上下文预算过小, 无法形成有效的触发和压缩目标');
    if (recent === undefined || !Number.isInteger(recent) || recent < 0) return setError('保留最近轮数必须是大于等于 0 的整数');
    if (draft.video_frame_interval === undefined || !Number.isFinite(draft.video_frame_interval) || draft.video_frame_interval < 0.5 || draft.video_frame_interval > 5) return setError('抽帧间隔必须在 0.5 至 5 秒之间');
    setError('');
    setSaving(true);
    try {
      const config: LLMConfig = { ...draft, name: draft.name.trim(), network_timeout: draft.network_timeout ?? 60, thinking_field_name: draft.thinking_field_name?.trim() || null };
      for (const key of ['model', 'base_url', 'api_key'] as const) {
        const value = config[key]?.trim();
        if (value) config[key] = value;
        else delete config[key];   // 空密钥不提交, 保留已有凭据
      }
      if (await onSave(config)) onClose();
      else setError('保存失败, 请检查配置或稍后重试');
    } catch (caught) {
      setError(`保存失败: ${caught instanceof Error ? caught.message : String(caught)}`);
    } finally {
      setSaving(false);
    }
  };

  return <FormModal open onClose={onClose} title={isNew ? '新增模型配置' : `编辑模型: ${modelName}`} size="lg"
    fields={fields} values={{ ...draft }} loading={saving} submitText={isNew ? '创建' : '保存修改'}
    onChange={(key, value) => { setDraft((previous) => ({ ...previous, [key]: value })); setError(''); }}
    onSubmit={() => { void handleSave(); }} statusContent={<>
      <div className="glass-card rounded-md px-3 py-2 text-xs text-text-tertiary grid grid-cols-2 gap-2" aria-label="上下文预算">
        <span>总窗口: {windowTokens.toLocaleString()}</span><span>历史预算: {historyBudget.toLocaleString()}</span>
        <span>触发处理: {triggerTokens.toLocaleString()}</span><span>处理目标: {floorTokens.toLocaleString()}</span>
        <span>输出预算: {(windowTokens - historyBudget).toLocaleString()}</span>
      </div>
      {error && <p role="alert" className="text-sm text-error">{error}</p>}
    </>} />;
}
