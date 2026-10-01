export function readEdictumParams(params: Record<string, unknown>, managePrompt: boolean) {
  const advanced = { ...params };
  if (managePrompt) delete advanced.system_prompt;
  return {
    system_prompt_enabled: managePrompt && typeof params.system_prompt === 'string',
    system_prompt: typeof params.system_prompt === 'string' ? params.system_prompt : '',
    params: JSON.stringify(advanced, null, 2),
  };
}

export function writeEdictumParams(form: {
  params: string;
  system_prompt_enabled: boolean;
  system_prompt: string;
}, managePrompt: boolean): Record<string, unknown> {
  const parsed: unknown = JSON.parse(form.params);
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new Error('参数必须是 JSON 对象');
  }
  const params = parsed as Record<string, unknown>;
  if (managePrompt) {
    if ('system_prompt' in params) throw new Error('请在系统提示词输入框中配置 system_prompt');
    if (form.system_prompt_enabled) params.system_prompt = form.system_prompt;
  }
  return params;
}
