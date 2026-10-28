import { useCallback, useEffect, useMemo, useRef, useState, type RefObject } from 'react';
import {
  AlertCircle, AlertTriangle, Check, Copy, FileCode2, FileText, GitCompare, RefreshCw, Save, Undo2,
} from 'lucide-react';
import { cn } from '@/utils/cn';
import { Badge, Button, IconButton, Modal, toast, type Tone } from '@/components/kit';
import { settingsApi, type YamlIssue } from '@/api/contracts';
import { errorText } from '@/utils/errorText';
import { Banner, SectionCard } from './shared';
import { compactDiff, diffLines, diffSummary, type DiffRow } from './types';

interface Props {
  i: number;
  active: boolean;
  draft: string;
  saved: string;
  revision: string;
  path: string;
  backendRunning: boolean;
  controlOnline: boolean;
  onChange: (value: string) => void;
  onSave: () => Promise<boolean>;
  onReload: () => void;
  onDirty: (dirty: boolean) => void;
  register: (actions: { save: () => Promise<boolean>; undo: () => void }) => void;
}

const LINE_HEIGHT = 24;

function YamlEditor({ value, onChange, errorLines, warningLines, textareaRef, onSaveShortcut, disabled }: {
  value: string;
  onChange: (value: string) => void;
  errorLines: Set<number>;
  warningLines: Set<number>;
  textareaRef: RefObject<HTMLTextAreaElement>;
  onSaveShortcut: () => void;
  disabled: boolean;
}) {
  const gutterRef = useRef<HTMLDivElement>(null);
  const lineCount = value.split('\n').length;
  const lines = Array.from({ length: lineCount }, (_, index) => index + 1);

  return (
    <div
      style={{ height: `clamp(240px, ${lineCount * LINE_HEIGHT + 26}px, 70vh)` }}
      className="relative flex overflow-hidden rounded-xl border border-line bg-[rgb(0_0_0/0.2)] [[data-theme=light]_&]:bg-white/55">
      <div aria-hidden className="relative h-full w-11 flex-none overflow-hidden border-r border-line bg-[rgb(0_0_0/0.12)] [[data-theme=light]_&]:bg-black/[0.03]">
        <div ref={gutterRef} className="py-3 will-change-transform">
          {lines.map((line) => (
            <div key={line}
              className={cn('h-6 px-2 text-right font-mono text-[11px] leading-6',
                errorLines.has(line) ? 'bg-[rgb(var(--rgb-red)/0.2)] font-medium text-red'
                  : warningLines.has(line) ? 'bg-[rgb(var(--rgb-orange)/0.16)] text-orange'
                    : 'text-fg-4')}>
              {line}
            </div>
          ))}
        </div>
      </div>
      <textarea
        ref={textareaRef}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onScroll={(event) => {
          if (gutterRef.current) gutterRef.current.style.transform = `translateY(${-event.currentTarget.scrollTop}px)`;
        }}
        onKeyDown={(event) => {
          if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
            event.preventDefault();
            onSaveShortcut();
            return;
          }
          if (event.key !== 'Tab') return;
          event.preventDefault();
          const element = event.currentTarget;
          const start = element.selectionStart;
          const end = element.selectionEnd;
          const next = `${value.slice(0, start)}  ${value.slice(end)}`;
          onChange(next);
          requestAnimationFrame(() => {
            element.selectionStart = start + 2;
            element.selectionEnd = start + 2;
          });
        }}
        aria-label="原始配置文件 (YAML)"
        spellCheck={false}
        autoCapitalize="off"
        autoCorrect="off"
        wrap="off"
        disabled={disabled}
        className="scroll h-full flex-1 resize-none bg-transparent px-3 py-3 font-mono text-[12.5px] leading-6 text-fg caret-[var(--fg-accent)] outline-none placeholder:text-fg-4 disabled:opacity-60"
        placeholder="# 配置文件内容"
      />
    </div>
  );
}

function positionLabel(issue: YamlIssue): string {
  if (issue.line === null) return '位置未知';
  return issue.column === null ? `第 ${issue.line} 行` : `第 ${issue.line} 行 · 第 ${issue.column} 列`;
}

function IssueList({ issues, onJump }: { issues: YamlIssue[]; onJump: (line: number) => void }) {
  return (
    <ul className="space-y-1">
      {issues.map((issue, index) => (
        <li key={`${issue.code}-${issue.line}-${index}`}>
          <button type="button" disabled={issue.line === null} onClick={() => { if (issue.line !== null) onJump(issue.line); }}
            className={cn('flex w-full items-start gap-2 rounded-lg px-2 py-1.5 text-left transition-colors hover:bg-glass-hover disabled:cursor-default disabled:hover:bg-transparent',
              issue.path === null ? 'text-orange' : 'text-red')}>
            <AlertCircle className="mt-0.5 h-3.5 w-3.5 flex-none" />
            <span className="min-w-0 flex-1 text-[12px]">
              {issue.message}
              {issue.path && <span className="ml-1.5 font-mono text-[10.5px] text-fg-4">{issue.path}</span>}
            </span>
            <span className="flex-none rounded bg-glass-hover px-1.5 font-mono text-[10.5px] text-fg-3">{positionLabel(issue)}</span>
          </button>
        </li>
      ))}
    </ul>
  );
}

function DiffRows({ rows }: { rows: DiffRow[] }) {
  return (
    <div className="scroll max-h-[52vh] overflow-auto rounded-xl border border-line bg-[rgb(0_0_0/0.18)] font-mono text-[11.5px] leading-5 [[data-theme=light]_&]:bg-white/60">
      {rows.map((row, index) => (
        <div key={index} className={cn('flex',
          row.kind === 'add' ? 'bg-[rgb(var(--rgb-green)/0.12)]'
            : row.kind === 'del' ? 'bg-[rgb(var(--rgb-red)/0.12)]' : '')}>
          <span className="w-9 flex-none select-none px-1 text-right text-fg-4">{row.oldLine ?? ''}</span>
          <span className="w-9 flex-none select-none px-1 text-right text-fg-4">{row.newLine ?? ''}</span>
          <span className={cn('w-5 flex-none select-none text-center',
            row.kind === 'add' ? 'text-green' : row.kind === 'del' ? 'text-red' : 'text-fg-4')}>
            {row.kind === 'add' ? '+' : row.kind === 'del' ? '-' : ' '}
          </span>
          <span className={cn('min-w-0 flex-1 whitespace-pre-wrap break-all px-2',
            row.kind === 'add' ? 'text-green' : row.kind === 'del' ? 'text-red' : 'text-fg-2')}>
            {row.text || ' '}
          </span>
        </div>
      ))}
    </div>
  );
}

export function RawConfigSection({
  i, active, draft, saved, revision, path, backendRunning, controlOnline, onChange, onSave, onReload, onDirty, register,
}: Props) {
  const [issues, setIssues] = useState<YamlIssue[]>([]);
  const [validated, setValidated] = useState(false);
  const [validating, setValidating] = useState(false);
  const [validationError, setValidationError] = useState('');
  const [saving, setSaving] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [comparison, setComparison] = useState(false);
  const [diffOpen, setDiffOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const baseRevisionRef = useRef(revision);

  const dirty = draft !== saved;
  const conflict = dirty && revision !== baseRevisionRef.current;
  const diff = useMemo(() => diffLines(saved, draft), [saved, draft]);
  const counts = useMemo(() => diffSummary(diff), [diff]);
  const previewRows = useMemo(() => compactDiff(diff, 2), [diff]);
  const lineCount = draft.split('\n').length;
  const errorLines = useMemo(() => new Set(issues.map((issue) => issue.line).filter((line): line is number => line !== null)), [issues]);

  useEffect(() => onDirty(dirty), [dirty, onDirty]);

  useEffect(() => {
    if (!dirty) baseRevisionRef.current = revision;
  }, [dirty, revision]);

  /* 服务端实时校验: 只有 YAML 契约返回完整结构时才展示结果, 避免旧后端被误判为语法错误 */
  // 滚动经过分节会反复激活; 同一份草稿已有校验结果时不再重复请求
  const validatedDraft = useRef<string | null>(null);
  useEffect(() => {
    if (!active || validatedDraft.current === draft) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      setValidating(true);
      void settingsApi.validateYaml(draft, controller.signal)
        .then((result) => {
          if (controller.signal.aborted) return;
          if (typeof result?.valid !== 'boolean' || !Array.isArray(result.errors)) {
            setValidated(false);
            setIssues([]);
            setValidationError('');
            return;
          }
          setValidated(true);
          setIssues(result.errors);
          setValidationError('');
          validatedDraft.current = draft;
        })
        .catch((cause) => {
          if (controller.signal.aborted) return;
          setValidated(false);
          setIssues([]);
          setValidationError(errorText(cause, '校验服务不可用'));
        })
        .finally(() => { if (!controller.signal.aborted) setValidating(false); });
    }, 450);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [active, draft]);

  const jumpTo = useCallback((line: number) => {
    const element = textareaRef.current;
    if (!element) return;
    const offset = draft.split('\n').slice(0, Math.max(0, line - 1)).join('\n').length + (line > 1 ? 1 : 0);
    element.focus();
    element.setSelectionRange(offset, offset);
    element.scrollTop = Math.max(0, (line - 3) * LINE_HEIGHT);
  }, [draft]);

  const performSave = useCallback(async (): Promise<boolean> => {
    if (!controlOnline) { toast.error('控制服务未运行或配置文件缺失'); return false; }
    if (validated && issues.length > 0) {
      toast.error(`原始配置存在 ${issues.length} 个错误`, '修正后才能保存');
      const first = issues.find((issue) => issue.line !== null);
      if (first?.line) jumpTo(first.line);
      return false;
    }
    if (!dirty) return true;
    if (conflict) {
      setComparison(true);
      toast.error('保存失败: 修订冲突', '配置文件已被其它会话修改, 请先读取最新版本');
      return false;
    }
    setSaving(true);
    try {
      const ok = await onSave();
      if (ok) {
        setDiffOpen(false);
        setComparison(false);
      }
      return ok;
    } finally {
      setSaving(false);
    }
  }, [controlOnline, validated, issues, dirty, conflict, jumpTo, onSave]);

  const undo = useCallback(() => {
    onChange(saved);
    setComparison(false);
    toast.success('已恢复到已保存的配置');
  }, [onChange, saved]);

  useEffect(() => { register({ save: performSave, undo }); }, [register, performSave, undo]);

  const refresh = () => {
    if (refreshing || saving) return;
    setRefreshing(true);
    onReload();
    window.setTimeout(() => {
      setRefreshing(false);
      if (dirty) {
        setComparison(true);
        toast.warning('已重新读取配置文件', '草稿已保留, 请核对差异后再保存');
      } else {
        setComparison(false);
        toast.success('已重新读取配置文件');
      }
    }, 400);
  };

  const discard = () => {
    onChange(saved);
    setComparison(false);
    toast.success('已载入磁盘上的已保存版本');
  };

  const copy = () => {
    void navigator.clipboard?.writeText(draft);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
    toast.success('配置内容已复制');
  };

  const saveClick = () => {
    if (!dirty) { toast.info('没有需要保存的修改'); return; }
    if (validated && issues.length > 0) {
      toast.error(`原始配置存在 ${issues.length} 个错误`, '修正后才能保存');
      const first = issues.find((issue) => issue.line !== null);
      if (first?.line) jumpTo(first.line);
      return;
    }
    if (conflict) {
      setComparison(true);
      toast.error('保存前需要处理修订冲突', '请先读取最新版本或放弃草稿');
      return;
    }
    setDiffOpen(true);
  };

  const statusTone: Tone = !validated ? 'neutral' : issues.length > 0 ? 'red' : 'green';
  const statusText = validating && !validated ? '校验中…'
    : !validated ? '等待校验'
      : issues.length > 0 ? `${issues.length} 个错误` : '校验通过';

  return (
    <SectionCard id="raw" i={i} tint="purple" icon={<FileCode2 className="h-4 w-4" />}
      title="原始配置" description="完整 YAML 配置。保存前会由控制服务校验语法与关键字段, 并展示与已保存版本的差异"
      actions={<>
        {dirty && <Badge tone="orange" dot>有未保存修改</Badge>}
        <Badge tone={statusTone} dot live={validating}>{statusText}</Badge>
        <Button size="sm" variant="ghost" icon={<RefreshCw className={cn('h-4 w-4', refreshing && 'animate-spin-slow')} />}
          onClick={refresh} disabled={refreshing || saving}>重新读取</Button>
        <Button size="sm" variant="ghost" icon={<Check className="h-4 w-4" />}
          onClick={() => {
            if (!validated) toast.info('尚未取得校验结果', '请稍候或检查控制服务');
            else if (issues.length > 0) toast.error(`校验未通过, ${issues.length} 个错误`, issues[0].message);
            else toast.success('校验通过', '配置语法与关键字段均有效');
          }}>校验</Button>
      </>}>

      <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-2 rounded-xl border border-line bg-glass px-3.5 py-2.5 text-[11.5px] text-fg-3">
        <span className="flex min-w-0 items-center gap-1.5">
          <FileText className="h-3.5 w-3.5 flex-none" />
          <code className="truncate font-mono text-[11.5px] text-fg-2" title={path || undefined}>{path || '尚未读取配置路径'}</code>
        </span>
        <span className="num">修订 <code className="font-mono text-fg-2">{revision}</code></span>
        <span className="num">{lineCount} 行</span>
        <span className="ml-auto text-fg-4">{backendRunning ? '保存后控制服务会重启后端' : '后端未运行, 保存后下次启动生效'}</span>
      </div>

      {!controlOnline && (
        <Banner tone="warning" title="控制服务未运行" className="mb-3">
          当前只能查看配置内容, 启动控制服务后才能校验并保存。
        </Banner>
      )}

      {conflict && (
        <Banner tone="error" title="修订冲突: 配置文件已在其它位置更新" className="mb-3"
          actions={<>
            <Button size="sm" variant="ghost" icon={<GitCompare className="h-3.5 w-3.5" />} onClick={() => setDiffOpen(true)}>查看差异</Button>
            <Button size="sm" variant="ghost" onClick={refresh}>重新读取最新配置</Button>
            <Button size="sm" variant="ghost" onClick={discard}>放弃草稿</Button>
          </>}>
          当前草稿基于修订 <code className="font-mono text-fg-2">{baseRevisionRef.current}</code>, 磁盘上已是 <code className="font-mono text-fg-2">{revision}</code>。
          保存会被拒绝, 请先比较差异, 再重新读取或放弃草稿。
        </Banner>
      )}
      {!conflict && comparison && (
        <Banner tone="warning" title="已读取最新版本, 编辑区仍是你的草稿" className="mb-3"
          actions={<Button size="sm" variant="ghost" onClick={discard}>放弃草稿</Button>}>
          差异预览基于本地已保存版本。确认合并无误后可以保存。
        </Banner>
      )}

      <div className="overflow-hidden rounded-xl border border-line bg-glass">
        <div className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2">
          <FileCode2 className="h-3.5 w-3.5 text-purple" />
          <span className="font-mono text-[11.5px] text-fg-2">config.yaml</span>
          <Badge tone="purple" className="!h-[18px] !text-[10.5px]">YAML</Badge>
          <span className="num text-[10.5px] text-fg-4">{lineCount} 行</span>
          {validated && issues.length > 0 && <span className="num text-[10.5px] text-red">{issues.length} 错误</span>}
          <div className="ml-auto flex items-center gap-1">
            <IconButton label={copied ? '已复制' : '复制内容'} size="sm" onClick={copy}>
              {copied ? <Check className="h-3.5 w-3.5 text-green" /> : <Copy className="h-3.5 w-3.5" />}
            </IconButton>
            <IconButton label="恢复到已保存版本" size="sm" onClick={undo} disabled={!dirty}>
              <Undo2 className="h-3.5 w-3.5" />
            </IconButton>
          </div>
        </div>
        <YamlEditor value={draft} onChange={onChange} errorLines={errorLines} warningLines={new Set()} textareaRef={textareaRef}
          onSaveShortcut={saveClick} disabled={saving} />
      </div>

      <div className="mt-3">
        {validated && issues.length === 0 ? (
          <p className="flex items-center gap-1.5 text-[12px] text-green">
            <Check className="h-3.5 w-3.5" />校验通过, 当前内容可以保存。
          </p>
        ) : (
          <div className="rounded-xl border border-line bg-glass px-2 py-2">
            <div className="mb-1 flex flex-wrap items-center gap-2 px-2">
              <span className="text-[12px] font-medium text-fg-2">校验结果</span>
              {!validated
                ? <Badge tone="neutral">{validating ? '校验中' : '等待校验'}</Badge>
                : <Badge tone="red">{issues.length} 错误</Badge>}
              {validated && issues.length > 0 && (
                <Button size="sm" variant="ghost" className="ml-auto !h-6"
                  onClick={() => { const first = issues.find((issue) => issue.line !== null); if (first?.line) jumpTo(first.line); }}>
                  跳到第一个错误
                </Button>
              )}
            </div>
            {validated && issues.length > 0
              ? <IssueList issues={issues} onJump={jumpTo} />
              : <p className="px-2 py-1 text-[11.5px] text-fg-4">
                {validating ? '正在通过控制服务校验当前内容…'
                  : validationError ? `暂时无法校验: ${validationError}`
                    : '修改内容后会自动校验, 校验结果会标注行号与列号。'}
              </p>}
            {validated && issues.length > 0 && (
              <p className="px-2 pt-1 text-[10.5px] text-fg-4">错误必须修正后才能保存。位置未知的条目无法自动定位。</p>
            )}
          </div>
        )}
      </div>

      {dirty && (
        <div className="mt-3 rounded-xl border border-line bg-glass p-3">
          <div className="flex flex-wrap items-center gap-2">
            <GitCompare className="h-4 w-4 text-purple" />
            <span className="text-[12.5px] font-medium text-fg-2">与已保存版本的差异</span>
            <Badge tone="green">+{counts.added}</Badge>
            <Badge tone="red">−{counts.removed}</Badge>
            <Button size="sm" variant="ghost" className="ml-auto !h-7" onClick={() => setDiffOpen(true)}>查看完整差异</Button>
          </div>
          <div className="mt-2 max-h-44 overflow-auto rounded-lg border border-line bg-[rgb(0_0_0/0.16)] py-1 font-mono text-[11px] leading-5 [[data-theme=light]_&]:bg-white/50">
            {previewRows.map((row, index) => (
              <div key={index} className={cn('flex px-2',
                row.kind === 'add' ? 'bg-[rgb(var(--rgb-green)/0.1)] text-green'
                  : row.kind === 'del' ? 'bg-[rgb(var(--rgb-red)/0.1)] text-red' : 'text-fg-4')}>
                <span className="w-4 flex-none select-none">{row.kind === 'add' ? '+' : row.kind === 'del' ? '-' : ' '}</span>
                <span className="min-w-0 flex-1 whitespace-pre-wrap break-all">{row.text || ' '}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="mt-5 flex flex-wrap items-center gap-2 border-t border-line pt-5">
        <Button variant="primary" icon={<Save className="h-4 w-4" />} loading={saving} disabled={!controlOnline || !dirty || (validated && issues.length > 0)}
          onClick={saveClick}>保存原始配置</Button>
        <Button variant="ghost" icon={<Undo2 className="h-4 w-4" />} disabled={!dirty || saving} onClick={undo}>撤销修改</Button>
        <span className="ml-auto text-[11.5px] text-fg-4">保存前会再次校验并展示差异; 使用 Ctrl+Enter 也可以保存</span>
      </div>

      <Modal open={diffOpen} onClose={() => setDiffOpen(false)} size="xl" title="保存前确认差异"
        description={`应用原始配置 · 新增 ${counts.added} 行 · 删除 ${counts.removed} 行`}
        icon={<GitCompare className="h-[18px] w-[18px]" />} className="tint-purple"
        footer={<>
          <Button variant="ghost" onClick={() => setDiffOpen(false)} disabled={saving}>取消</Button>
          <Button variant="primary" icon={<Save className="h-4 w-4" />} loading={saving}
            disabled={validated && issues.length > 0} onClick={() => void performSave()}>确认保存</Button>
        </>}>
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <Badge tone="green" dot>+{counts.added} 行</Badge>
          <Badge tone="red" dot>−{counts.removed} 行</Badge>
          <Badge tone="purple">基于修订 {revision}</Badge>
          <span className="text-[11.5px] text-fg-4">仅显示差异附近的行</span>
        </div>
        <DiffRows rows={previewRows} />
        {backendRunning && (
          <p className="mt-3 flex items-start gap-1.5 text-[12px] text-orange">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 flex-none" />
            后端正在运行, 保存后会自动重启, 进行中的会话会被中断。
          </p>
        )}
        {validated && issues.length > 0 && (
          <p className="mt-3 flex items-start gap-1.5 text-[12px] text-red">
            <AlertCircle className="mt-0.5 h-3.5 w-3.5 flex-none" />
            仍有 {issues.length} 个错误, 需要修正后才能保存。
          </p>
        )}
      </Modal>
    </SectionCard>
  );
}
