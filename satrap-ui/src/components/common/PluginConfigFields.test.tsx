import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { PluginConfigFields } from './PluginConfigFields';
import { pluginOptionNeeds } from '@/hooks/usePluginConfigOptions';

describe('插件选项状态', () => {
  const schema = { note: { type: 'string', default: '', description: '' }, model: { type: 'llm', default: '', description: '' } };
  it('选项未就绪时保留原值, 不误报引用失效, 普通参数仍可编辑', () => {
    const markup = renderToStaticMarkup(<PluginConfigFields schema={schema} values={{ note: '草稿', model: 'saved' }} modelState="loading" onChange={() => undefined} />);
    expect(markup).toContain('正在读取选项');
    expect(markup).not.toContain('引用失效');
    expect(markup).toMatch(/<option[^>]*value="saved"[^>]*selected/);
    expect(markup.match(/<input[^>]*value="草稿"[^>]*>/)?.[0]).not.toContain('disabled');
  });
  it('成功读取后才标记确实缺失的引用', () => {
    const markup = renderToStaticMarkup(<PluginConfigFields schema={schema} values={{ model: 'gone' }} onChange={() => undefined} />);
    expect(markup).toContain('gone (引用失效)');
  });
  it('选项需求仅取决于引用字段, 普通配置不读取模型或知识库', () => {
    expect(pluginOptionNeeds({ note: schema.note })).toEqual({ models: false, knowledge: false });
    expect(pluginOptionNeeds(schema)).toEqual({ models: true, knowledge: false });
    expect(pluginOptionNeeds({ kb: { type: 'knowledge_bases', default: [], description: '' } })).toEqual({ models: false, knowledge: true });
  });
});
