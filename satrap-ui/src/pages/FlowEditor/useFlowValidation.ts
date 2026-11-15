import { useEffect, useRef, useState } from 'react';
import { edictumApi } from '@/api/edictum';
import type { FlowDefinitionJSON, FlowIssue, FlowValidateResult } from '@/api/types';
import type { ValidateState } from './Inspector';
import { createValidationScheduler, type ValidationScheduler } from './validationScheduler';

/** 检查请求的附加条件 */
export interface ValidateOptions {
  /** 会话会用在哪些对话类型上 */
  kinds: Array<'group' | 'private'>;
  /** 可选的目标平台, 用于检查后台发送能力 */
  platformId: string;
}

/**
 * 编辑后延迟检查; 新的编辑会中止在途请求
 *
 * validateNow 立即检查并返回结果, 服务不可用或被中止时返回 null
 */
export function useFlowValidation(definition: FlowDefinitionJSON | null, options: ValidateOptions, delay = 600) {
  const [issues, setIssues] = useState<FlowIssue[]>([]);
  const [state, setState] = useState<ValidateState>('idle');
  const latest = useRef({ definition, options });
  latest.current = { definition, options };
  // 画布位置不影响检查结果, 拖动节点不触发请求
  const key = definition ? JSON.stringify([{ ...definition, layout: undefined }, options.kinds, options.platformId]) : '';

  const scheduler = useRef<ValidationScheduler<FlowValidateResult> | null>(null);
  if (scheduler.current === null) {
    scheduler.current = createValidationScheduler<FlowValidateResult>({
      delay,
      run: (signal) => {
        const { definition: flow, options: current } = latest.current;
        setState('checking');
        return edictumApi.validateFlow({
          flow: flow ?? { nodes: [] },
          conversation_kinds: current.kinds,
          ...(current.platformId ? { platform_id: current.platformId } : {}),
        }, signal);
      },
      onResult: (result) => { setIssues(result.issues); setState('ready'); },
      onError: () => { setIssues([]); setState('unavailable'); },
    });
  }

  useEffect(() => { if (key) scheduler.current?.schedule(); }, [key]);
  useEffect(() => () => scheduler.current?.dispose(), []);

  return { issues, state, validateNow: () => scheduler.current!.runNow() };
}
