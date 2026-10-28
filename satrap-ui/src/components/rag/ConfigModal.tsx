import { useState } from 'react';
import { AlertCircle, Ban, Database, RotateCw } from 'lucide-react';
import { cn } from '@/utils/cn';
import { Badge } from '@/components/kit/Badge';
import { Button } from '@/components/kit/Button';
import { Field, Input } from '@/components/kit/Field';
import { Modal } from '@/components/kit/Modal';
import { Switch } from '@/components/kit/Switch';
import type { ModelOptions } from '@/components/common/PluginConfigFields';
import { defaultConfig, embedOptions, type ConfigContext, type Kb, type KBConfig, type KBScope, type KBDraft } from './types';
import { Select as KitSelect } from '@/components/kit/Select';

interface Form {
  name: string;
  scope: KBScope;
  embed: string;
  chunk_size: string;
  chunk_overlap: string;
  batch_size: string;
  duplicate_policy: 'skip' | 'replace';
  is_default: boolean;
}

interface Props {
  target: Kb | 'new';
  context: ConfigContext;
  modelOptions?: ModelOptions;
  onClose: () => void;
  onSave: (draft: KBDraft, expectedRevision: number) => Promise<string | null>;
  onRebuild: (config: KBConfig) => void;
}

export function ConfigModal({ target, context, modelOptions, onClose, onSave, onRebuild }: Props) {
  const existing = target === 'new' ? null : target;
  const [form, setForm] = useState<Form>(() => existing ? {
    name: existing.name,
    scope: existing.scope,
    embed: existing.config.embed,
    chunk_size: String(existing.config.chunk_size),
    chunk_overlap: String(existing.config.chunk_overlap),
    batch_size: String(existing.config.batch_size),
    duplicate_policy: existing.config.duplicate_policy,
    is_default: existing.isDefault,
  } : {
    ...defaultConfig,
    name: '',
    scope: context.sessionId ? 'session' : 'global',
    chunk_size: String(defaultConfig.chunk_size),
    chunk_overlap: String(defaultConfig.chunk_overlap),
    batch_size: String(defaultConfig.batch_size),
    is_default: false,
  } as Form);
  const [touched, setTouched] = useState(false);
  const [saving, setSaving] = useState(false);
  const [serverError, setServerError] = useState('');

  const set = <K extends keyof Form>(key: K, value: Form[K]) => setForm((current) => ({ ...current, [key]: value }));
  const chunkSize = Number(form.chunk_size);
  const overlap = Number(form.chunk_overlap);
  const batch = Number(form.batch_size);

  const nameInvalid = !form.name.trim() || form.name.trim().length > 120;
  const chunkInvalid = !Number.isInteger(chunkSize) || chunkSize < 1 || chunkSize > 100000;
  const overlapInvalid = !Number.isInteger(overlap) || overlap < 0 || overlap >= chunkSize;
  const batchInvalid = !Number.isInteger(batch) || batch < 1 || batch > 1000;
  const nameError = touched ? (!form.name.trim() ? '请填写知识库名称' : form.name.trim().length > 120 ? '名称不能超过 120 个字符' : undefined) : undefined;
  const embedError = touched && !form.embed ? '请选择 Embedding 模型' : undefined;
  const chunkError = touched && chunkInvalid ? '需为 1–100000 的整数' : undefined;
  const overlapError = touched && overlapInvalid ? '需为 0 到小于分块大小的整数' : undefined;
  const batchError = touched && batchInvalid ? '需为 1–1000 的整数' : undefined;
  const valid = !nameInvalid && Boolean(form.embed) && !chunkInvalid && !overlapInvalid && !batchInvalid;

  const needsRebuild = Boolean(existing && (
    form.embed !== existing.config.embed
    || chunkSize !== existing.config.chunk_size
    || overlap !== existing.config.chunk_overlap
  ));

  const config = (): KBConfig => ({
    ...defaultConfig,
    embed: form.embed,
    chunk_size: Number.isFinite(chunkSize) ? chunkSize : 0,
    chunk_overlap: Number.isFinite(overlap) ? overlap : 0,
    batch_size: Number.isFinite(batch) ? batch : 0,
    duplicate_policy: form.duplicate_policy,
  });

  const save = async () => {
    setTouched(true);
    if (!valid || saving) return;
    setSaving(true);
    setServerError('');
    const error = await onSave(
      { name: form.name.trim(), scope: form.scope, config: config(), isDefault: form.is_default },
      existing?.revision ?? 0,
    );
    setSaving(false);
    if (error) setServerError(error);
    else onClose();
  };

  const rebuild = () => {
    setTouched(true);
    if (!valid) return;
    onRebuild(config());
  };

  const embed = embedOptions(modelOptions, existing?.config.embed);

  return (
    <Modal
      open
      onClose={() => { if (!saving) onClose(); }}
      title={existing ? '知识库配置' : '新建知识库'}
      description={existing ? '更新名称、导入策略与检索参数' : `当前管理范围：${context.label}`}
      icon={<Database className="h-[18px] w-[18px]" />}
      size="lg"
      className="tint-purple"
      footer={<>
        <Button variant="ghost" disabled={saving} onClick={onClose}>取消</Button>
        {existing && (
          <Button variant="tinted" icon={<RotateCw className="h-4 w-4" />} disabled={saving || !valid || existing.status === 'indexing' || existing.documentCount === 0} onClick={rebuild}>重建索引</Button>
        )}
        <Button variant="primary" loading={saving} onClick={() => void save()}>保存</Button>
      </>}
    >
      <div className="space-y-4">
        {serverError && (
          <div role="alert" className="flex items-start gap-2 rounded-xl border border-[rgb(var(--rgb-red)/0.3)] bg-[rgb(var(--rgb-red)/0.09)] px-3 py-2.5 text-[13px] text-red">
            <AlertCircle className="mt-0.5 h-4 w-4 flex-none" aria-hidden />
            <span className="leading-5">{serverError}</span>
          </div>
        )}

        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="知识库名称" required error={nameError} className="sm:col-span-2">
            <Input
              value={form.name}
              onChange={(event) => set('name', event.target.value)}
              onBlur={() => setTouched(true)}
              placeholder="例如：客服主群 · 退款政策"
              maxLength={120}
              data-autofocus
            />
          </Field>

          {!existing && (
            <Field
              label="层级"
              required
              hint={context.sessionId ? '会话库只对当前会话可见；全局库对全部会话可见' : '当前管理范围没有具体会话，只能创建全局库'}
              className="sm:col-span-2"
            >
              <KitSelect className="field" value={form.scope} onChange={(value) => set('scope', value as KBScope)}>
                <option value="global">全局库 · 所有会话可检索</option>
                {context.sessionId && <option value="session">当前会话库 · 仅 {context.sessionId} 可见</option>}
              </KitSelect>
            </Field>
          )}

          <Field label="Embedding 模型" required error={embedError} hint="决定向量空间；更换后必须重建索引">
            <KitSelect className="field" value={form.embed} onChange={(value) => set('embed', value)}>
              <option value="">选择向量模型</option>
              {embed.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
            </KitSelect>
          </Field>

          <Field label="重复来源策略" hint={form.duplicate_policy === 'skip' ? '同名来源跳过导入，保留原文档' : '同名来源整体替换，旧片段被移除'}>
            <KitSelect className="field" value={form.duplicate_policy} onChange={(value) => set('duplicate_policy', value as 'skip' | 'replace')}>
              <option value="skip">跳过重复来源</option>
              <option value="replace">替换已有文档</option>
            </KitSelect>
          </Field>

          <Field label="分块大小" error={chunkError} hint="每块字符数，越大上下文越完整">
            <Input type="number" min={1} max={100000} step={1} value={form.chunk_size} onChange={(event) => set('chunk_size', event.target.value)} />
          </Field>

          <Field label="块重叠" error={overlapError} hint="相邻块重叠字符数，避免语义被截断">
            <Input type="number" min={0} step={1} value={form.chunk_overlap} onChange={(event) => set('chunk_overlap', event.target.value)} />
          </Field>

          <Field label="批大小" error={batchError} hint="每批向量化的片段数">
            <Input type="number" min={1} max={1000} step={1} value={form.batch_size} onChange={(event) => set('batch_size', event.target.value)} />
          </Field>
        </div>

        {existing && existing.scope === 'session' && (
          <div className="rounded-xl border border-line bg-glass px-4">
            <Field inline label="作为此会话的默认知识库" hint="会话未显式选择知识库时，检索与写入默认使用此库">
              <Switch label="作为此会话的默认知识库" checked={form.is_default} onChange={(value) => set('is_default', value)} disabled={saving} />
            </Field>
          </div>
        )}

        {needsRebuild && (
          <div className="flex items-start gap-2.5 rounded-xl border border-[rgb(var(--rgb-orange)/0.28)] bg-[rgb(var(--rgb-orange)/0.08)] px-3 py-2.5 text-[13px] leading-5 tint-orange">
            <Ban className="mt-0.5 h-4 w-4 flex-none text-tint" aria-hidden />
            <span className="text-fg-2">
              模型或分块参数已修改，保存时后端会拒绝并要求显式重建。点击 <span className="font-medium text-tint">重建索引</span> 会使用新参数重新向量化全部文档；成功后原子切换索引，失败时保留原配置与数据。
            </span>
          </div>
        )}

        {existing && (
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-xl border border-line bg-glass px-3.5 py-2.5 text-[11.5px] text-fg-3">
            <Badge tone="purple">v{existing.revision}</Badge>
            <span className="num">{existing.documentCount} 份文档 · {existing.chunkCount} 个片段</span>
            <span className="num">分块 {existing.config.chunk_size} / 重叠 {existing.config.chunk_overlap}</span>
            <span className={cn('num ml-auto', needsRebuild ? 'text-orange' : 'text-green')}>
              {needsRebuild ? '等待重建' : '配置与索引一致'}
            </span>
          </div>
        )}

        <p className="text-[11.5px] leading-5 text-fg-4">
          重复来源按来源名称判断；批大小与重复策略可即时保存，模型或分块变更必须重建。
        </p>
      </div>
    </Modal>
  );
}
