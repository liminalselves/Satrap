import type { KnowledgeBase, RagContext, UploadCapabilities } from '@/api/rag';
import { ImportModal } from '@/components/rag/ImportModal';
import { toKb, type ImportSummary } from '@/components/rag/types';

interface Props {
  context: RagContext;
  target: KnowledgeBase;
  capabilities: UploadCapabilities;
  onClose: () => void;
  onComplete: (result: Record<string, unknown>) => void;
}

/** 兼容旧调用方: 新的导入弹窗以页面模型为输入, 回执同时保留旧的 summary/source/chunks 字段 */
export function RagImportModal({ context, target, capabilities, onClose, onComplete }: Props) {
  return <ImportModal
    context={context}
    target={toKb(target)}
    capabilities={capabilities}
    onClose={onClose}
    onComplete={(summary: ImportSummary) => onComplete({
      ...summary,
      status: summary.failed > 0 ? 'partial' : summary.indexed > 0 ? 'indexed' : 'skipped',
      summary: `本次导入：成功 ${summary.indexed}，跳过 ${summary.skipped}，失败 ${summary.failed}`,
      source: summary.items[0]?.source,
      chunks: summary.chunks,
    })}
  />;
}
