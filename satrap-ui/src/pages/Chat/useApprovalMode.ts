import { useCallback, useEffect, useRef, useState } from 'react';
import { chatApi, type ApprovalMode, type ApprovalState } from '@/api/chat';

// 设置页与输入框快捷入口共用服务端确认的模式, 不各自维护缓存
export function useApprovalMode(conversationId: string) {
  const [state, setState] = useState<ApprovalState | null>(null);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const generation = useRef(0);
  const reads = useRef(0);
  const pendingSave = useRef(false);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    if (!conversationId || pendingSave.current) return;
    const version = generation.current;
    const request = ++reads.current;
    try {
      const result = await chatApi.getApproval(conversationId, signal);
      if (version === generation.current && request === reads.current) {
        setState(result);
        setError('');
      }
    } catch (reason) {
      if (!signal?.aborted && version === generation.current && request === reads.current) {
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    }
  }, [conversationId]);

  useEffect(() => {
    ++generation.current;
    pendingSave.current = false;
    setState(null);
    setError('');
    setSaving(false);
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => { generation.current++; reads.current++; controller.abort(); };
  }, [refresh]);

  const save = async (mode: ApprovalMode) => {
    if (!state?.available || pendingSave.current || mode === state.mode) return;
    const version = generation.current;
    pendingSave.current = true;
    ++reads.current;   // 保存开始后, 旧读取结果不得覆盖已确认的新模式
    setSaving(true);
    setError('');
    try {
      const result = await chatApi.setApproval(conversationId, mode, state.revision);
      if (version === generation.current) setState(result);
    } catch (reason) {
      if (version !== generation.current) return;
      setError(reason instanceof Error ? reason.message : String(reason));
      try {
        const latest = await chatApi.getApproval(conversationId);
        if (version === generation.current) setState(latest);
      } catch { /* 保留保存错误, 不显示未经服务端确认的新模式 */ }
    } finally {
      if (version === generation.current) {
        pendingSave.current = false;
        setSaving(false);
      }
    }
  };

  return { conversationId, state, error, saving, save, refresh };
}

export type ApprovalControl = ReturnType<typeof useApprovalMode>;
