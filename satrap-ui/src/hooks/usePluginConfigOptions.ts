import { useEffect, useState } from 'react';
import type { EdictumPluginConfigField } from '@/api/types';
import type { ConfigOption, ModelOptions } from '@/components/common/PluginConfigFields';
import { pluginKnowledgeRead, pluginModelRead } from '@/api/pluginReads';
import { errorText } from '@/utils/errorText';

export function pluginOptionNeeds(schema: Record<string, EdictumPluginConfigField> | undefined) {
  const fields = Object.values(schema || {});
  return { models: fields.some((field) => ['llm', 'embed', 'rerank', 'asr'].includes(field.type)),
    knowledge: fields.some((field) => ['knowledge_base', 'knowledge_bases'].includes(field.type)) };
}

export function usePluginConfigOptions(schema: Record<string, EdictumPluginConfigField> | undefined, active: boolean) {
  const needs = pluginOptionNeeds(schema);
  const wantModels = active && needs.models, wantKnowledge = active && needs.knowledge;
  const [models, setModels] = useState<ModelOptions>();
  const [bases, setBases] = useState<ConfigOption[]>();
  const [modelError, setModelError] = useState('');
  const [knowledgeError, setKnowledgeError] = useState('');
  useEffect(() => {
    setModels(undefined); setModelError('');
    if (!wantModels) return;
    const controller = new AbortController();
    pluginModelRead.read({ signal: controller.signal }).then((result) => { if (!controller.signal.aborted) setModels(result.options); })
      .catch((error: unknown) => { if (!controller.signal.aborted) setModelError(errorText(error)); });
    return () => controller.abort();
  }, [wantModels]);
  useEffect(() => {
    setBases(undefined); setKnowledgeError('');
    if (!wantKnowledge) return;
    const controller = new AbortController();
    pluginKnowledgeRead.read({ signal: controller.signal }).then((result) => {
      if (!controller.signal.aborted) setBases(result.knowledge_bases.filter((base) => base.scope === 'global').map((base) => ({ value: base.id, label: base.name, scope: base.scope })));
    }).catch((error: unknown) => { if (!controller.signal.aborted) setKnowledgeError(errorText(error)); });
    return () => controller.abort();
  }, [wantKnowledge]);
  return { models: models || {}, bases: bases || [], modelError, knowledgeError,
    modelState: wantModels && !models ? modelError ? 'error' as const : 'loading' as const : 'ready' as const,
    knowledgeState: wantKnowledge && !bases ? knowledgeError ? 'error' as const : 'loading' as const : 'ready' as const };
}
