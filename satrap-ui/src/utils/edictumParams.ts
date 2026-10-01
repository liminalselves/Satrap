export function readEdictumParams(params: Record<string, unknown>, managePrompt: boolean) {
  const advanced = { ...params };
  const modelParams = typeof params.model_params === 'object' && params.model_params !== null
    ? params.model_params as Record<string, unknown> : {};
  if (managePrompt) {
    delete advanced.system_prompt;
    delete advanced.thinking;
    delete advanced.model_params;
    for (const key of ['temperature', 'top_p', 'max_tokens']) delete advanced[key];
  }
  return {
    system_prompt_enabled: managePrompt && typeof params.system_prompt === 'string',
    system_prompt: typeof params.system_prompt === 'string' ? params.system_prompt : '',
    thinking: typeof params.thinking === 'string' ? params.thinking : '',
    temperature: (modelParams.temperature ?? params.temperature) as number | undefined,
    top_p: (modelParams.top_p ?? params.top_p) as number | undefined,
    max_tokens: (modelParams.max_tokens ?? params.max_tokens) as number | undefined,
    params: JSON.stringify(advanced, null, 2),
  };
}

export function writeEdictumParams(form: {
  params: string;
  system_prompt_enabled: boolean;
  system_prompt: string;
  thinking?: string;
  temperature?: number;
  top_p?: number;
  max_tokens?: number;
}, managePrompt: boolean): Record<string, unknown> {
  const parsed: unknown = JSON.parse(form.params);
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new Error('参数必须是 JSON 对象');
  }
  const params = parsed as Record<string, unknown>;
  if (managePrompt) {
    if (['system_prompt', 'thinking', 'model_params', 'temperature', 'top_p', 'max_tokens'].some((key) => key in params)) {
      throw new Error('请在系统提示词和模型参数输入框中配置这些字段');
    }
    if (form.system_prompt_enabled) params.system_prompt = form.system_prompt;
    if (form.thinking) params.thinking = form.thinking;
    const modelParams: Record<string, number> = {};
    for (const key of ['temperature', 'top_p', 'max_tokens'] as const) {
      const value = form[key];
      if (value !== undefined) {
        if (!Number.isFinite(value)) throw new Error(`${key} 必须是有限数字`);
        modelParams[key] = value;
      }
    }
    if (form.temperature !== undefined && (form.temperature < 0 || form.temperature > 2)) throw new Error('温度必须在 0 到 2 之间');
    if (form.top_p !== undefined && (form.top_p <= 0 || form.top_p > 1)) throw new Error('top_p 必须大于 0 且不超过 1');
    if (form.max_tokens !== undefined && (!Number.isInteger(form.max_tokens) || form.max_tokens <= 0)) throw new Error('最大输出 token 数必须是正整数');
    if (Object.keys(modelParams).length) params.model_params = modelParams;
  }
  return params;
}
