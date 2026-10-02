import { Link } from 'react-router-dom';
import type { AgentBinding, EdictumSessionConfig, SessionClassConfig } from '@/api/types';

export interface AgentOption {
  provider: 'session_class' | 'edictum';
  name: string;
  enabled: boolean;
  summary: string;
}

export function agentOptions(classes: Record<string, SessionClassConfig>, edictum: Record<string, EdictumSessionConfig>): AgentOption[] {
  return [
    ...Object.entries(classes).map(([name, config]) => {
      const model = config.params?.[config.model_key || 'model_name'];
      return {
        provider: 'session_class' as const, name, enabled: config.enabled,
        summary: `模型: ${typeof model === 'string' && model ? model : '由流程决定'} · 插件: 由流程决定`,
      };
    }),
    ...Object.entries(edictum).map(([name, config]) => ({
      provider: 'edictum' as const, name, enabled: config.enabled,
      summary: `模型: ${config.model_name || '未配置'} · 插件: ${(config.plugins || []).filter((item) => typeof item === 'string' || item.enabled !== false).length}`,
    })),
  ].sort((a, b) => a.name.localeCompare(b.name));
}

export function bindingError(bindings: Record<string, AgentBinding>, kinds: Record<string, string>, options: AgentOption[], declarationKnown: boolean): string {
  for (const [kind, binding] of Object.entries(bindings)) {
    const label = kinds[kind] || kind;
    if (declarationKnown && !(kind in kinds)) return `${label} 已不受当前适配器支持, 请删除该绑定`;
    if (binding.mode === 'inherit') continue;
    if (!binding.config_name.trim()) return `${label} Agent 必须选择命名配置`;
    const option = options.find((item) => item.provider === binding.provider && item.name === binding.config_name);
    if (!option) return `${label} Agent 配置已删除, 请重新选择`;
    if (!option.enabled) return `${label} Agent 配置已停用, 请重新选择`;
  }
  return '';
}

interface Props {
  bindings: Record<string, AgentBinding>;
  kinds: Record<string, string>;
  options: AgentOption[];
  defaultProvider: string;
  defaultName: string;
  declarationError?: string;
  onChange: (bindings: Record<string, AgentBinding>) => void;
}

export function AgentRoutingEditor({ bindings, kinds, options, defaultProvider, defaultName, declarationError, onChange }: Props) {
  const keys = Array.from(new Set([...Object.keys(kinds), ...Object.keys(bindings)]));
  const optionValue = (provider: string, name: string) => JSON.stringify([provider, name]);
  const inherited = options.find((item) => item.provider === defaultProvider && item.name === defaultName);
  return <div className="space-y-3 rounded-lg border border-glass-border p-3" data-testid="agent-routing">
    <p className="text-sm text-text-secondary">未指定的对话类型继承平台默认。群内单独指定的 Agent 优先于此处设置</p>
    <p className="text-sm text-text-secondary">指定类型后, 私聊与群聊使用独立上下文。切换绑定保留旧记录, 后续请求使用新会话</p>
    {declarationError && <p role="alert" className="text-sm text-warning">{declarationError}</p>}
    {keys.map((kind) => {
      const label = kinds[kind] || kind;
      const binding = bindings[kind] || { mode: 'inherit' as const };
      const supported = kind in kinds;
      const selected = binding.mode === 'value' ? options.find((item) => item.provider === binding.provider && item.name === binding.config_name) : undefined;
      return <fieldset key={kind} className="space-y-2 rounded border border-glass-border p-3">
        <legend className="px-1 text-sm font-medium">{label} Agent</legend>
        {!supported && <div className="flex flex-wrap items-center gap-2 text-sm text-warning">
          <span>当前适配器未声明此类型, 已保存的绑定仍保留</span>
          <button type="button" className="text-accent hover:underline" onClick={() => {
            const next = { ...bindings };
            delete next[kind];
            onChange(next);
          }}>删除 {label} 绑定</button>
        </div>}
        <label className="block text-sm">{label} Agent 来源
          <select aria-label={`${label} Agent 来源`} className="glass-input mt-1 w-full" value={binding.mode} onChange={(event) => onChange({
            ...bindings, [kind]: event.target.value === 'inherit' ? { mode: 'inherit' }
              : { mode: 'value', provider: defaultProvider === 'edictum' ? 'edictum' : 'session_class', config_name: '' },
          })}>
            <option value="inherit">继承平台默认</option>
            <option value="value">指定 Agent 配置</option>
          </select>
        </label>
        {binding.mode === 'value' ? <>
          <label className="block text-sm">{label} Agent 配置
            <select aria-label={`${label} Agent 配置`} className="glass-input mt-1 w-full" value={optionValue(binding.provider, binding.config_name)} onChange={(event) => {
              const [provider, name] = JSON.parse(event.target.value) as [AgentOption['provider'], string];
              onChange({ ...bindings, [kind]: { mode: 'value', provider, config_name: name } });
            }}>
              {!binding.config_name && <option value={optionValue(binding.provider, '')}>请选择 Agent 配置</option>}
              {options.map((option) => <option key={optionValue(option.provider, option.name)} value={optionValue(option.provider, option.name)} disabled={!option.enabled}>
                {option.name} · {option.provider === 'edictum' ? 'Edictum' : 'SessionClass'}{option.enabled ? '' : ' · 已停用'}
              </option>)}
              {binding.config_name && !selected && <option value={optionValue(binding.provider, binding.config_name)} disabled>{binding.config_name} · 已删除</option>}
            </select>
          </label>
          <p className={`text-sm ${selected?.enabled ? 'text-text-secondary' : 'text-warning'}`}>
            {selected ? `${selected.summary}${selected.enabled ? '' : ' · 所选配置已停用'}` : binding.config_name ? '所选配置已删除, 请重新选择' : '尚未选择配置'}
          </p>
        </> : <div className="space-y-1 text-sm text-text-secondary">
          <p>实际配置: {defaultProvider === 'edictum' ? 'Edictum' : 'SessionClass'} / {defaultName || '同名会话类或全局默认'}</p>
          {inherited && <p>{inherited.summary}{inherited.enabled ? '' : ' · 已停用'}</p>}
        </div>}
      </fieldset>;
    })}
    <Link to="/agents" className="text-sm text-accent hover:underline">管理 Agent 的模型、提示词和插件</Link>
  </div>;
}
