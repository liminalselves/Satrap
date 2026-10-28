import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import {
  Bot, Boxes, BrainCircuit, Code2, Info, MessageSquareText, PauseCircle, Puzzle, Save, Sparkles, X,
} from 'lucide-react';
import { Badge } from '@/components/kit/Badge';
import { Button, IconButton } from '@/components/kit/Button';
import { Drawer } from '@/components/kit/Drawer';
import { Field, Input, Textarea } from '@/components/kit/Field';
import { Switch } from '@/components/kit/Switch';
import { toast } from '@/components/kit/Toast';
import { cn } from '@/utils/cn';
import { getThinkingOptions } from '@/utils/constants';
import { readEdictumParams, writeEdictumParams } from '@/utils/edictumParams';
import type { EdictumSessionConfig, EdictumTypeDefinition, LLMConfig } from '@/api/types';
import { providerLabel, providerTone, type AgentEntry, type Provider } from './shared';
import { Select as KitSelect } from '@/components/kit/Select';

const MANAGED_KEYS = [
  'system_prompt', 'thinking', 'command_history_enabled', 'default_agent_prompt_enabled',
  'model_params', 'temperature', 'top_p', 'max_tokens',
];

const managesPrompt = (typeName: string): boolean => ['simple', 'async_simple'].includes(typeName);

interface Draft {
  name: string;
  enabled: boolean;
  description: string;
  class_path: string;
  is_async: boolean;
  context_key: string;
  model_key: string;
  model_name: string;
  edictum_type: string;
  system_prompt_enabled: boolean;
  system_prompt: string;
  command_history_enabled: boolean;
  default_agent_prompt_enabled: boolean;
  thinking: string;
  temperature: string;
  top_p: string;
  max_tokens: string;
  params: string;
}

export interface AgentSaveInput {
  provider: Provider;
  name: string;
  enabled: boolean;
  description: string;
  model_name: string;
  class_path?: string;
  is_async?: boolean;
  context_key?: string;
  model_key?: string;
  edictum_type?: string;
  params: Record<string, unknown>;
  plugins: EdictumSessionConfig['plugins'];
}

export interface AgentEditorProps {
  open: boolean;
  mode: 'create' | 'edit';
  provider: Provider;
  initial: AgentEntry | null;
  plugins: EdictumSessionConfig['plugins'];
  types: EdictumTypeDefinition[];
  llmNames: string[];
  llmConfigs: Record<string, LLMConfig>;
  saving: boolean;
  statusContent?: ReactNode;
  onClose: () => void;
  onSave: (input: AgentSaveInput) => void;
  onManagePlugins: (name: string) => void;
}

const numberText = (value: unknown): string => (
  typeof value === 'number' && Number.isFinite(value) ? String(value) : ''
);

function toDraft(entry: AgentEntry | null, provider: Provider, types: EdictumTypeDefinition[], llmNames: string[]): Draft {
  const typeName = entry?.edictum_type ?? (provider === 'edictum' ? types[0]?.name ?? '' : '');
  const manage = provider === 'edictum' && managesPrompt(typeName);
  const params = entry?.params ?? {};
  const read = readEdictumParams(params, manage);
  const sessionParams = provider === 'session_class' ? JSON.stringify(params, null, 2) : read.params;
  return {
    name: entry?.name ?? '',
    enabled: entry?.enabled ?? true,
    description: entry?.description ?? '',
    class_path: entry?.class_path ?? '',
    is_async: entry?.is_async ?? false,
    context_key: entry?.context_key ?? '',
    model_key: entry?.model_key ?? (provider === 'session_class' ? 'model_name' : ''),
    model_name: entry?.model_name ?? (llmNames[0] ?? ''),
    edictum_type: typeName,
    system_prompt_enabled: read.system_prompt_enabled,
    system_prompt: read.system_prompt,
    command_history_enabled: read.command_history_enabled,
    default_agent_prompt_enabled: read.default_agent_prompt_enabled,
    thinking: read.thinking,
    temperature: numberText(read.temperature),
    top_p: numberText(read.top_p),
    max_tokens: numberText(read.max_tokens),
    params: sessionParams,
  };
}

function Section({ icon, title, description, children, inactive }: {
  icon: ReactNode; title: string; description?: string; children: ReactNode; inactive?: boolean;
}) {
  return (
    <section className={cn('rounded-2xl border border-line bg-glass p-4 animate-rise', inactive && 'opacity-60')}>
      <div className="mb-3 flex items-start gap-2.5">
        <span className="mt-0.5 grid h-7 w-7 flex-none place-items-center rounded-lg bg-[rgb(var(--tint)/0.14)] text-tint">{icon}</span>
        <div className="min-w-0">
          <h3 className="text-[13.5px] font-semibold text-fg">{title}</h3>
          {description && <p className="mt-0.5 text-[11.5px] leading-4 text-fg-3">{description}</p>}
        </div>
      </div>
      <div className="space-y-3">{children}</div>
    </section>
  );
}

export function AgentEditor({
  open, mode, provider, initial, plugins, types, llmNames, llmConfigs, saving, statusContent, onClose, onSave, onManagePlugins,
}: AgentEditorProps) {
  const baseline = useMemo(
    () => toDraft(initial, provider, types, llmNames),
    [initial, provider, types, llmNames],
  );
  const [draft, setDraft] = useState<Draft>(baseline);
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!open) return;
    const next = toDraft(initial, provider, types, llmNames);
    setDraft(next);
    setDirty(false);
    setError('');
    // 只在重新打开时重置草稿: 后台目录刷新不覆盖未保存输入
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, initial?.name, provider]);

  const isEdictum = provider === 'edictum';
  const managePrompt = isEdictum && managesPrompt(draft.edictum_type);
  const typeDef = types.find((item) => item.name === draft.edictum_type);
  const thinkingOptions = getThinkingOptions(llmConfigs[draft.model_name]);
  const thinkingUnsupported = Boolean(draft.thinking) && managePrompt
    && !thinkingOptions.some((option) => option.value === draft.thinking);
  const legacyWakePolicy = useMemo(() => {
    try {
      const parsed: unknown = JSON.parse(draft.params || '{}');
      if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) return null;
      const policy = (parsed as Record<string, unknown>).wake_policy;
      return policy && typeof policy === 'object' ? policy : null;
    } catch {
      return null;
    }
  }, [draft.params]);

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => {
    setDraft((current) => ({ ...current, [key]: value }));
    setDirty(true);
    setError('');
  };

  const changeType = (next: string) => {
    const currentType = draft.edictum_type;
    try {
      const parsed = writeEdictumParams(buildWriteForm(draft, currentType), managesPrompt(currentType));
      const read = readEdictumParams(parsed, managesPrompt(next));
      setDraft((current) => ({
        ...current,
        edictum_type: next,
        system_prompt_enabled: read.system_prompt_enabled,
        system_prompt: read.system_prompt,
        command_history_enabled: read.command_history_enabled,
        default_agent_prompt_enabled: read.default_agent_prompt_enabled,
        thinking: read.thinking,
        temperature: numberText(read.temperature),
        top_p: numberText(read.top_p),
        max_tokens: numberText(read.max_tokens),
        params: read.params,
      }));
      setDirty(true);
      setError('');
    } catch (reason) {
      toast.error('切换类型失败', reason instanceof Error ? reason.message : '会话参数无效');
    }
  };

  const submit = () => {
    if (!draft.name.trim()) { setError('请填写配置名称'); return; }
    if (isEdictum && !draft.edictum_type.trim()) { setError('请选择 Edictum 类型'); return; }
    if (!isEdictum && !draft.class_path.trim()) { setError('名称和 Class Path 为必填项'); return; }
    if (thinkingUnsupported) { setError('绑定模型不支持当前思考强度, 请重新选择'); return; }

    let params: Record<string, unknown>;
    try {
      const parsed: unknown = JSON.parse(draft.params || '{}');
      if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) throw new Error('参数必须是 JSON 对象');
      params = parsed as Record<string, unknown>;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '参数必须是 JSON 对象');
      return;
    }

    if (isEdictum) {
      const conflict = Object.keys(params).find((key) => MANAGED_KEYS.includes(key));
      if (managePrompt && conflict) {
        setError(`请在提示词或模型参数输入框中配置 ${conflict}`);
        return;
      }
      try {
        params = writeEdictumParams(buildWriteForm(draft, draft.edictum_type), managePrompt);
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : '会话参数无效');
        return;
      }
    } else if (draft.model_key.trim() && draft.model_name) {
      params[draft.model_key.trim()] = draft.model_name;
    }

    setError('');
    onSave({
      provider,
      name: draft.name.trim(),
      enabled: draft.enabled,
      description: draft.description.trim(),
      model_name: draft.model_name,
      class_path: isEdictum ? undefined : draft.class_path.trim(),
      is_async: isEdictum ? undefined : draft.is_async,
      context_key: isEdictum ? undefined : draft.context_key.trim(),
      model_key: isEdictum ? undefined : draft.model_key.trim(),
      edictum_type: isEdictum ? draft.edictum_type : undefined,
      params,
      plugins,
    });
  };

  return (
    <Drawer open={open} onClose={() => { if (!saving) onClose(); }} label={mode === 'edit' ? `编辑 ${draft.name || 'Agent 配置'}` : '新建 Agent 配置'} width={820} className="tint-teal">
      <div className="flex items-start gap-3 border-b border-line px-5 py-4">
        <div className="grid h-10 w-10 flex-none place-items-center rounded-xl bg-[rgb(var(--tint)/0.16)] text-tint shadow-[inset_0_1px_0_var(--glass-highlight)]">
          {isEdictum ? <Bot className="h-5 w-5" /> : <Code2 className="h-5 w-5" />}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="truncate text-[16px] font-semibold text-fg">
              {mode === 'edit' ? draft.name || '未命名配置' : isEdictum ? '新建 Edictum 配置' : '注册会话类'}
            </h2>
            <Badge tone={providerTone(provider)}>{providerLabel(provider)}</Badge>
            {!draft.enabled && <Badge tone="orange">已停用</Badge>}
          </div>
          <p className="mt-0.5 text-xs text-fg-3">
            {isEdictum ? '配置模板、模型、提示词与执行流程; 保存后协调活跃会话' : '注册 Python 会话类模板, 供平台实例创建时使用'}
            {initial && (initial.activeInstances ?? 0) > 0 ? ` · ${initial.activeInstances} 个活跃实例` : ''}
          </p>
        </div>
        <div className="flex flex-none items-center gap-3">
          <label className="flex items-center gap-2 text-xs text-fg-3">
            <Switch label="启用配置" size="sm" checked={draft.enabled} onChange={(value) => set('enabled', value)} disabled={saving} />
            {draft.enabled ? '启用' : '停用'}
          </label>
          <IconButton label="关闭编辑抽屉" shortcut="Esc" size="sm" onClick={onClose} disabled={saving}><X className="h-4 w-4" /></IconButton>
        </div>
      </div>

      <div className="scroll min-h-0 flex-1 space-y-4 px-5 py-5">
        {error && (
          <div role="alert" className="tint-red rounded-xl border border-[rgb(var(--tint)/0.25)] bg-[rgb(var(--tint)/0.08)] px-3 py-2 text-xs text-fg-2">
            {error}
          </div>
        )}
        {statusContent}
        <Section icon={<Info className="h-3.5 w-3.5" />} title="基本" description="名称、类型与平台会话所需的标识">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="配置名称" required hint={isEdictum ? '用于平台绑定与运行时创建' : '保存后作为会话类名称'}>
              <Input value={draft.name} onChange={(event) => set('name', event.target.value)} placeholder={isEdictum ? '如: platform-assistant' : '如: qq-group-agent'} data-autofocus />
            </Field>
            {isEdictum ? (
              <Field label="Edictum 类型" required hint={typeDef?.description || '类型由后端注册表提供'}>
                <KitSelect className="field w-full" value={draft.edictum_type} onChange={(value) => changeType(value)} disabled={saving}>
                  {types.map((item) => (
                    <option key={item.name} value={item.name}>{`${item.name} (${item.is_async ? '异步' : '同步'})`}</option>
                  ))}
                </KitSelect>
              </Field>
            ) : (
              <Field label="Class Path" required hint="Python 类路径, 扫描结果会自动填充">
                <Input value={draft.class_path} onChange={(event) => set('class_path', event.target.value)} placeholder="如: misskey_session.MisskeySession" />
              </Field>
            )}
          </div>
          {!isEdictum && (
            <div className="grid gap-3 sm:grid-cols-3">
              <Field label="上下文键" hint="用于隔离同一平台上的不同上下文">
                <Input value={draft.context_key} onChange={(event) => set('context_key', event.target.value)} placeholder="如: group_id" />
              </Field>
              <Field label="模型键" hint="写入 params 的模型字段名">
                <Input value={draft.model_key} onChange={(event) => set('model_key', event.target.value)} placeholder="如: model" />
              </Field>
              <Field inline label="异步会话类" hint="AsyncSession">
                <input
                  type="checkbox"
                  className="h-4 w-4 accent-[rgb(var(--rgb-teal))]"
                  checked={draft.is_async}
                  disabled={saving}
                  onChange={(event) => set('is_async', event.target.checked)}
                />
              </Field>
            </div>
          )}
          <Field label="描述" hint="说明该配置的用途, 会显示在 Agent 卡片与实例列表中">
            <Textarea rows={2} value={draft.description} onChange={(event) => set('description', event.target.value)} placeholder="例如: QQ 群客服助手, 检索产品手册并保留命令历史" />
          </Field>
          {isEdictum && typeDef && (
            <div className="flex flex-wrap items-center gap-2 rounded-xl border border-line bg-glass px-3 py-2 text-xs text-fg-3">
              <span className="font-mono text-fg-2">{typeDef.name}</span>
              <Badge tone="teal">{typeDef.is_async ? 'async' : 'sync'}</Badge>
              {typeDef.capabilities.plugins && <Badge>插件</Badge>}
              {typeDef.capabilities.mcp && <Badge>MCP</Badge>}
              {typeDef.capabilities.stream && <Badge>流式</Badge>}
              {initial && (initial.activeInstances ?? 0) > 0 && (
                <span className="ml-auto text-orange">保存后立即协调 {initial.activeInstances} 个活跃会话</span>
              )}
            </div>
          )}
        </Section>

        <Section icon={<Sparkles className="h-3.5 w-3.5" />} title="模型" description="绑定 LLM 与默认采样参数; 留空则继承模型配置">
          <Field label="绑定 LLM" hint={draft.model_name ? `当前绑定 ${draft.model_name}` : '未绑定时使用平台默认模型'}>
            <KitSelect className="field w-full" value={draft.model_name} onChange={(value) => set('model_name', value)} disabled={saving}>
              <option value="">暂不绑定</option>
              {llmNames.map((name) => <option key={name} value={name}>{name}</option>)}
            </KitSelect>
          </Field>
          {isEdictum && (
            <Field label="默认思考强度" hint={draft.model_name ? '可选值取决于绑定模型的思考能力' : '绑定模型后可选择思考强度'}>
              <KitSelect className="field w-full" value={draft.thinking} onChange={(value) => set('thinking', value)} disabled={saving}>
                <option value="">默认 (关闭)</option>
                {thinkingOptions.filter((option) => option.value !== 'off').map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
                {managePrompt && thinkingUnsupported && (
                  <option value={draft.thinking}>{draft.thinking} (当前模型不支持, 请重新选择)</option>
                )}
              </KitSelect>
            </Field>
          )}
          {isEdictum && managePrompt && (
            <div className="grid gap-3 sm:grid-cols-3">
              <Field label="温度" hint="0 – 2, 留空继承">
                <Input type="number" min={0} max={2} step={0.01} value={draft.temperature} onChange={(event) => set('temperature', event.target.value)} placeholder="继承模型配置" />
              </Field>
              <Field label="top_p" hint="0 – 1, 留空继承">
                <Input type="number" min={0} max={1} step={0.01} value={draft.top_p} onChange={(event) => set('top_p', event.target.value)} placeholder="继承模型配置" />
              </Field>
              <Field label="最大输出 token 数" hint="正整数, 留空继承">
                <Input type="number" min={1} step={1} value={draft.max_tokens} onChange={(event) => set('max_tokens', event.target.value)} placeholder="继承模型配置" />
              </Field>
            </div>
          )}
        </Section>

        {isEdictum && managePrompt && (
          <Section icon={<MessageSquareText className="h-3.5 w-3.5" />} title="提示词与上下文" description="系统提示词、内置 Agent 规则与命令历史">
            <Field inline label="配置系统提示词" hint="启用后使用下方文本; 清空保存会删除已有提示词">
              <input
                type="checkbox"
                className="h-4 w-4 accent-[rgb(var(--rgb-teal))]"
                aria-label="配置系统提示词"
                checked={draft.system_prompt_enabled}
                disabled={saving}
                onChange={(event) => set('system_prompt_enabled', event.target.checked)}
              />
            </Field>
            <Textarea
              rows={5}
              value={draft.system_prompt}
              disabled={!draft.system_prompt_enabled || saving}
              onChange={(event) => set('system_prompt', event.target.value)}
              placeholder="填写 bot 的角色、行为要求和回复风格"
              aria-label="系统提示词"
            />
            <div className="divide-y divide-[var(--glass-border)] rounded-xl border border-line bg-glass px-3">
              <Field inline label="使用内置 Agent 提示" hint="补充工具调用与结果核实的通用规则, 可与自定义提示同时使用" className="py-2.5">
                <input
                  type="checkbox"
                  className="h-4 w-4 accent-[rgb(var(--rgb-teal))]"
                  aria-label="使用内置 Agent 提示"
                  checked={draft.default_agent_prompt_enabled}
                  disabled={saving}
                  onChange={(event) => set('default_agent_prompt_enabled', event.target.checked)}
                />
              </Field>
              <Field inline label="命令写入上下文" hint="让 AI 在后续对话中看到已执行命令及回复, 不会额外调用模型" className="py-2.5">
                <input
                  type="checkbox"
                  className="h-4 w-4 accent-[rgb(var(--rgb-teal))]"
                  aria-label="命令写入上下文"
                  checked={draft.command_history_enabled}
                  disabled={saving}
                  onChange={(event) => set('command_history_enabled', event.target.checked)}
                />
              </Field>
            </div>
          </Section>
        )}

        {isEdictum && (
          <Section icon={<Puzzle className="h-3.5 w-3.5" />} title="插件" description="插件配置属于当前命名会话, 保存后由运行时同步到活跃会话" inactive={mode === 'create'}>
            {plugins.length === 0 ? (
              <div className="flex items-center justify-between gap-3 rounded-xl border border-dashed border-[var(--glass-border-strong)] px-3.5 py-3">
                <p className="text-[12.5px] text-fg-3">尚未配置插件. 可添加记忆、联网检索、MCP 桥接等能力.</p>
                <Button size="sm" variant="tinted" tint="teal" icon={<Puzzle className="h-3.5 w-3.5" />} disabled={mode === 'create' || !initial} onClick={() => initial && onManagePlugins(initial.name)}>管理</Button>
              </div>
            ) : (
              <>
                <div className="flex flex-wrap gap-1.5">
                  {plugins.map((item) => {
                    const name = typeof item === 'string' ? item : item.name;
                    const enabled = typeof item === 'string' || item.enabled !== false;
                    const configCount = typeof item === 'string' ? 0 : Object.keys(item.config || {}).length;
                    return (
                      <span key={name} className={cn('chip', !enabled && 'opacity-60')}>
                        {enabled ? <span className="dot !h-1.5 !w-1.5 tint-green" /> : <PauseCircle className="h-3 w-3" />}
                        {name}
                        {configCount > 0 && <span className="text-fg-4">· {configCount} 项配置</span>}
                      </span>
                    );
                  })}
                </div>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <p className="text-[11px] text-fg-4">
                    {mode === 'create' || !initial
                      ? '保存当前配置后可管理插件'
                      : '插件修改单独保存, 不会影响当前未保存的表单'}
                  </p>
                  <Button size="sm" variant="tinted" tint="teal" icon={<Puzzle className="h-3.5 w-3.5" />} disabled={mode === 'create' || !initial || saving} onClick={() => initial && onManagePlugins(initial.name)}>
                    管理插件
                  </Button>
                </div>
              </>
            )}
          </Section>
        )}

        <Section icon={<BrainCircuit className="h-3.5 w-3.5" />} title="唤醒策略" description="主动参与、冷却与回复偏好由平台设置统一管理">
          <p className="rounded-xl border border-line bg-glass px-3 py-2 text-[11.5px] leading-5 text-fg-3">
            唤醒策略保存在平台的 <code className="rounded bg-glass-hover px-1 font-mono text-fg-2">wake-policy-contract</code> 设置中,
            可按平台、群与时段覆盖. 此处只展示当前会话参数里的历史透传值, 编辑请前往
            <Link to="/platforms" className="ml-1 text-accent hover:underline">平台设置</Link>.
          </p>
          {legacyWakePolicy && (
            <pre className="scroll max-h-40 rounded-xl border border-line bg-[rgb(0_0_0/0.18)] p-3 font-mono text-[11px] leading-5 text-fg-2 [[data-theme=light]_&]:bg-[rgb(15_23_42/0.04)]">
              {JSON.stringify(legacyWakePolicy, null, 2)}
            </pre>
          )}
        </Section>

        <Section
          icon={<Boxes className="h-3.5 w-3.5" />}
          title="参数"
          description={isEdictum ? '高级会话参数, 系统提示词与模型参数已在上面单独配置' : '会话类初始化参数; 模型字段会按模型键自动写入'}
        >
          <Field
            label={isEdictum ? '其他会话参数 (JSON 对象)' : '参数 (JSON 对象)'}
            hint={isEdictum ? '已排除 system_prompt / thinking / model_params 等托管字段' : '与 Class Path 的 init_params 合并'}
          >
            <Textarea
              rows={9}
              value={draft.params}
              onChange={(event) => set('params', event.target.value)}
              spellCheck={false}
              className="font-mono text-[12px]"
              aria-label={isEdictum ? '其他会话参数 (JSON 对象)' : '参数 (JSON 对象)'}
            />
          </Field>
          {!isEdictum && (
            <p className="text-[11px] text-fg-4">模型键为 <code className="font-mono">{draft.model_key || '未设置'}</code>, 绑定的 LLM 会自动写入该字段.</p>
          )}
        </Section>
      </div>

      <div className="border-t border-line px-5 py-3.5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex min-w-0 items-center gap-2 text-xs">
            {dirty ? (
              <span className="flex items-center gap-2 text-orange"><span className="dot tint-orange" data-live="1" />有未保存的修改</span>
            ) : (
              <span className="text-fg-4">{mode === 'create' ? '填写后创建配置' : '所有修改已同步'}</span>
            )}
            {saving && <span className="text-fg-3">正在保存并协调运行时…</span>}
          </div>
          <div className="flex items-center gap-2">
            <Button variant="ghost" onClick={onClose} disabled={saving}>取消</Button>
            <Button variant="tinted" tint="teal" icon={<Save className="h-4 w-4" />} loading={saving} onClick={submit}>
              {mode === 'create' ? '创建' : '保存'}
            </Button>
          </div>
        </div>
      </div>
    </Drawer>
  );
}

function buildWriteForm(draft: Draft, typeName: string) {
  const optional = (raw: string) => {
    if (raw.trim() === '') return undefined;
    const value = Number(raw);
    return Number.isFinite(value) ? value : undefined;
  };
  return {
    params: draft.params,
    system_prompt_enabled: draft.system_prompt_enabled,
    system_prompt: draft.system_prompt,
    thinking: managesPrompt(typeName) ? draft.thinking : undefined,
    command_history_enabled: draft.command_history_enabled,
    default_agent_prompt_enabled: draft.default_agent_prompt_enabled,
    temperature: optional(draft.temperature),
    top_p: optional(draft.top_p),
    max_tokens: optional(draft.max_tokens),
  };
}
