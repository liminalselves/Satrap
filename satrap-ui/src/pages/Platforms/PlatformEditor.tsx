import { useMemo } from 'react';
import { AlertTriangle, CheckCircle2, Clock3, Copy, Save, X, Zap } from 'lucide-react';
import { cn } from '@/utils/cn';
import { Badge, Button, Drawer, Field, IconButton, Input, Switch, Textarea } from '@/components/kit';
import type { FormField } from '@/components/common';
import { Note, PlatformIcon, SectionHead } from './shared';

interface PlatformEditorProps {
  open: boolean;
  onClose: () => void;
  title: string;
  type: string;
  initial: boolean;
  revision: string;
  draftRevision: string;
  dirty: boolean;
  saving: boolean;
  conflict: boolean;
  fields: FormField[];
  values: Record<string, unknown>;
  onChange: (key: string, value: unknown) => void;
  onSubmit: () => void;
  onCopyDraft: () => void;
}

interface SectionDef { key: string; title: string; description: string; tint: 'pink' | 'teal' | 'orange' }

// 字段分区: 与预览的编辑分区同构, 但保留一次性可见的完整表单,
// 以免已有键盘/回归路径依赖的标签被隐藏
const SECTIONS: SectionDef[] = [
  { key: 'basic', title: '基本配置', description: '平台标识、默认流程与档案保留', tint: 'pink' },
  { key: 'connection', title: '连接参数', description: '协议接入、账号与上下文范围', tint: 'teal' },
  { key: 'message', title: '消息与媒体', description: '文本预算、引用回源与语音/附件处理', tint: 'pink' },
  { key: 'wake', title: '唤醒策略', description: '自动参与、唤醒词与回复偏好', tint: 'orange' },
  { key: 'overrides', title: '群级与时段覆盖', description: '在平台策略之上对特定群或时段生效', tint: 'teal' },
  { key: 'dryrun', title: '唤醒试算', description: '隔离窗口判断, 不写入配置', tint: 'pink' },
  { key: 'settings', title: '适配器 Settings', description: '该类型暂无结构化表单, 直接编辑 JSON 对象', tint: 'pink' },
];

const SECTION_BY_KEY: Record<string, string> = {
  enable: 'basic', id: 'basic', type: 'basic', session_provider: 'basic', session_type: 'basic',
  session_bindings: 'basic', 'settings.message_archive_retention_days': 'basic',
  'settings.host': 'connection', 'settings.port': 'connection', 'settings.access_token': 'connection',
  'settings.secret': 'connection', 'settings.self_id': 'connection', 'settings.enable_private': 'connection',
  'settings.enable_group': 'connection', 'settings.context_scope': 'connection', 'settings.group_whitelist': 'connection',
  'settings.base_url': 'connection', 'settings.api_token': 'connection', 'settings.max_message_length': 'connection',
  'settings.misskey_default_visibility': 'connection', 'settings.misskey_local_only': 'connection',
  'settings.chat_enabled': 'connection', 'settings.room_enabled': 'connection',
  'settings.message_text_limit': 'message', 'settings.input_text_limit': 'message', 'settings.input_media_limit': 'message',
  'settings.asr_model': 'message', 'settings.voice_transcribe': 'message', 'settings.attachment_extract': 'message',
  'settings.media_insecure_tls': 'message', 'settings.media_plaintext_http': 'message',
  'settings.media_trusted_hosts': 'message', 'settings.command_operators': 'message', 'settings.notice_types': 'message',
  'settings.reply_with_quote': 'wake', 'settings.reply_with_mention': 'wake', 'settings.quote_lookup': 'wake',
  'settings.forward_lookup': 'wake', 'settings.wake_on_quote_self': 'wake', 'settings.wake_mode': 'wake',
  'settings.wake_mode_note': 'wake', 'settings.wake_message_threshold': 'wake', 'settings.wake_score_threshold': 'wake',
  'settings.wake_max_wait': 'wake', 'settings.wake_cooldown': 'wake', 'settings.wake_talk_value': 'wake',
  'settings.wake_words': 'wake', 'settings.wake_aliases': 'wake',
  'settings.wake_group_overrides': 'overrides', 'settings.wake_time_rules': 'overrides',
  'settings.wake_dry_run_preview': 'dryrun', settings_json: 'settings',
};

function CheckboxField({ field, value, onChange }: { field: FormField; value: unknown; onChange: (value: unknown) => void }) {
  return (
    <div className={cn('flex items-center justify-between gap-4 rounded-xl border border-line bg-glass px-3.5 py-2.5', field.disabled && 'opacity-60')}>
      <span className="min-w-0">
        <span className="block text-[13px] font-medium text-fg-2">{field.label}</span>
        {field.placeholder && <span className="mt-0.5 block text-[11.5px] text-fg-3">{field.placeholder}</span>}
      </span>
      <Switch label={field.label} checked={Boolean(value)} disabled={field.disabled} onChange={onChange} />
    </div>
  );
}

function FieldControl({ field, value, onChange }: {
  field: FormField; value: unknown; onChange: (value: unknown) => void;
}) {
  if (field.type === 'checkbox') return <CheckboxField field={field} value={value} onChange={onChange} />;
  if (field.type === 'custom') {
    const content = field.render ? field.render(value, onChange) : null;
    if (!content) return null;
    return (
      <div className="space-y-2">
        {field.label === '每条消息触发说明' ? null : <p className="text-[13px] font-medium text-fg-2">{field.label}</p>}
        {content}
      </div>
    );
  }
  if (field.type === 'select') {
    return (
      <Field label={field.label} required={field.required}>
        <select
          className="field w-full"
          value={String(value ?? '')}
          disabled={field.disabled}
          onChange={(event) => onChange(event.target.value)}
        >
          {field.options?.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
        </select>
      </Field>
    );
  }
  if (field.type === 'number') {
    return (
      <Field label={field.label} required={field.required}>
        <Input
          type="number"
          value={value === undefined || value === null ? '' : String(value)}
          placeholder={field.placeholder}
          disabled={field.disabled}
          min={field.min}
          max={field.max}
          step={field.step ?? 'any'}
          onChange={(event) => onChange(event.target.value === '' ? undefined : Number(event.target.value))}
        />
      </Field>
    );
  }
  if (field.type === 'textarea') {
    return (
      <Field label={field.label} required={field.required}>
        <Textarea
          rows={field.rows || 4}
          value={(value as string) ?? ''}
          placeholder={field.placeholder}
          disabled={field.disabled}
          onChange={(event) => onChange(event.target.value)}
          className="font-mono !text-[12.5px]"
        />
      </Field>
    );
  }
  return (
    <Field label={field.label} required={field.required}>
      <Input
        type={field.type === 'password' ? 'password' : 'text'}
        value={(value as string) ?? ''}
        placeholder={field.placeholder}
        disabled={field.disabled}
        onChange={(event) => onChange(event.target.value)}
      />
    </Field>
  );
}

export function PlatformEditor({
  open, onClose, title, type, initial, revision, draftRevision, dirty, saving, conflict, fields, values, onChange, onSubmit, onCopyDraft,
}: PlatformEditorProps) {
  const groups = useMemo(() => {
    const byKey = new Map<string, FormField[]>();
    for (const field of fields) {
      const sectionKey = SECTION_BY_KEY[field.key] || 'basic';
      const list = byKey.get(sectionKey) || [];
      list.push(field);
      byKey.set(sectionKey, list);
    }
    return SECTIONS
      .map((section) => ({ section, fields: byKey.get(section.key) || [] }))
      .filter((group) => group.fields.length > 0);
  }, [fields]);

  return (
    <Drawer open={open} onClose={onClose} label={title} width={880}>
      <div className="flex items-start gap-3 border-b border-line px-5 py-4 tint-pink">
        <PlatformIcon type={type} size="md" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-[16px] font-semibold text-fg">{title}</h2>
            <Badge tone="pink">{type}</Badge>
            {dirty && <Badge tone="orange" dot>未保存</Badge>}
          </div>
          <p className="mt-0.5 text-[12.5px] text-fg-3">
            {initial ? '修改后立即写入配置并在运行时热更新' : '保存后加入平台配置, 启动后端即可连接'}
          </p>
        </div>
        <IconButton label="关闭" shortcut="Esc" size="sm" onClick={onClose}><X className="h-4 w-4" /></IconButton>
      </div>

      <div className="scroll min-h-0 flex-1 px-5 py-4">
        {/* 分区快捷导航: 保留一次性可见的完整表单, 同时提供预览的分区结构 */}
        <div className="-mx-1 mb-4 flex gap-1.5 overflow-x-auto px-1 pb-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden" aria-label="平台编辑分区">
          {groups.map((group) => (
            <button key={group.section.key} type="button"
              onClick={() => document.getElementById(`platform-editor-${group.section.key}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })}
              className={cn('shrink-0 rounded-full border border-line bg-glass px-2.5 py-1 text-[12px] text-fg-3 transition hover:bg-glass-hover hover:text-fg')}>
              {group.section.title}
              {group.fields.length > 0 && <span className="num ml-1 text-[10.5px] text-fg-4">{group.fields.length}</span>}
            </button>
          ))}
        </div>
        {conflict && (
          <Note tone="orange" title="配置已变化, 草稿仍保留" className="mb-4"
            icon={<AlertTriangle className="h-4 w-4" />}
            action={<Button size="sm" variant="ghost" icon={<Copy className="h-3.5 w-3.5" />} onClick={onCopyDraft}>复制我的草稿</Button>}>
            不会自动覆盖其他管理员的修改; 可复制草稿后刷新平台列表再合并。
          </Note>
        )}
        <div className="space-y-4">
          {groups.map((group) => (
            <section key={group.section.key} id={`platform-editor-${group.section.key}`} className={cn('surface scroll-mt-2 !rounded-xl p-4', `tint-${group.section.tint}`)}>
              <SectionHead tint={group.section.tint} title={group.section.title} description={group.section.description}
                icon={group.section.key === 'wake' ? <Zap className="h-4 w-4" /> : undefined} />
              <div className={cn('grid gap-3', group.section.key === 'basic' ? 'sm:grid-cols-2' : 'sm:grid-cols-2')}>
                {group.fields.map((field) => (
                  <div key={field.key} className={cn(field.type === 'custom' || field.type === 'textarea' || field.key === 'settings_json' ? 'sm:col-span-2' : undefined)}>
                    <FieldControl field={field} value={values[field.key]} onChange={(next) => onChange(field.key, next)} />
                  </div>
                ))}
              </div>
            </section>
          ))}
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line px-5 py-3.5">
        <div className="flex items-center gap-2 text-[12px] text-fg-3">
          {dirty ? <><Clock3 className="h-3.5 w-3.5 text-orange" />有未保存修改</> : <><CheckCircle2 className="h-3.5 w-3.5 text-green" />已同步服务器</>}
          <span className="text-fg-4">· 修订 {draftRevision || revision || '未知'}</span>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="ghost" onClick={onClose}>取消</Button>
          <Button variant="primary" icon={<Save className="h-4 w-4" />} loading={saving} onClick={onSubmit}>
            {initial ? '保存修改' : '创建'}
          </Button>
        </div>
      </div>
    </Drawer>
  );
}
