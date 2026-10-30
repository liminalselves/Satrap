import { describe, expect, it } from 'vitest';
import {
  buildOutline, buildPages, extractReferences, headingId, parseFrontmatter, resolveImage, resolveLink,
} from './docModel';

describe('docModel', () => {
  it('builds a nested outline whose ids match rendered headings', () => {
    const body = [
      '# 标题', '### 孤立三级', '## 第一节', '### 子节 `code`', '```', '## 代码里的', '```',
      '## [链接](./a.md) **加粗**', '#### 四级忽略',
    ].join('\n');
    const outline = buildOutline(body);
    expect(outline.map((item) => item.text)).toEqual(['孤立三级', '第一节', '链接 加粗']);
    expect(outline[0].level).toBe(2);
    expect(outline[1].children).toEqual([{ id: headingId('子节 code'), text: '子节 code', level: 3, children: [] }]);
    expect(outline[2].id).toBe(headingId('链接 加粗'));
  });

  it('parses frontmatter and rejects unknown fields', () => {
    expect(parseFrontmatter('---\ntitle: "上手"\norder: 5\n---\n# 正文')).toEqual({ meta: { title: '上手', order: 5 }, body: '# 正文' });
    expect(parseFrontmatter('# 无头')).toEqual({ meta: {}, body: '# 无头' });
    expect(() => parseFrontmatter('---\nauthor: x\n---\n')).toThrow(/不支持/);
    expect(() => parseFrontmatter('---\norder: first\n---\n')).toThrow(/数字/);
  });

  it('orders pages and falls back to the first heading for titles', () => {
    const pages = buildPages([
      { path: 'b.md', raw: '# 乙' },
      { path: 'a.md', raw: '---\norder: 5\n---\n# 甲' },
      { path: 'index.md', raw: '无标题正文' },
    ]);
    expect(pages.map((page) => [page.slug, page.title])).toEqual([['index', 'index'], ['a', '甲'], ['b', '乙']]);
  });

  it('drops a leading h1 that repeats the page title', () => {
    const [same] = buildPages([{ path: 'a.md', raw: '---\ntitle: 甲\n---\n\n# 甲\n正文' }]);
    expect(same.body).toBe('正文');
    const [different] = buildPages([{ path: 'b.md', raw: '---\ntitle: 乙\n---\n# 另一个\n正文' }]);
    expect(different.body).toBe('# 另一个\n正文');
  });

  it('builds heading ids that keep Chinese text', () => {
    expect(headingId('第一次 使用!')).toBe('第一次-使用');
    expect(headingId('Chat 对话')).toBe('chat-对话');
  });

  it('classifies links and keeps them inside docs/user', () => {
    const slugs = new Set(['index', 'guide/plugins']);
    expect(resolveLink('index.md', 'https://example.com', slugs)).toEqual({ kind: 'external', href: 'https://example.com' });
    expect(resolveLink('index.md', '#插件', slugs)).toEqual({ kind: 'anchor', hash: '插件' });
    expect(resolveLink('index.md', './guide/plugins.md#状态', slugs)).toEqual({ kind: 'page', slug: 'guide/plugins', hash: '状态' });
    expect(resolveLink('guide/plugins.md', '../index.md', slugs)).toEqual({ kind: 'page', slug: 'index', hash: '' });
    expect(resolveLink('index.md', '../README.md', slugs).kind).toBe('invalid');
    expect(resolveLink('index.md', './missing.md', slugs).kind).toBe('invalid');
    expect(resolveLink('index.md', 'javascript:alert(1)', slugs).kind).toBe('invalid');
    expect(resolveImage('guide/plugins.md', '../images/a.png')).toBe('images/a.png');
    expect(resolveImage('index.md', '../outside.png')).toBeNull();
    expect(resolveImage('index.md', 'https://example.com/a.png')).toBeNull();
  });

  it('ignores references inside code', () => {
    expect(extractReferences('[a](x.md) `[b](y.md)`\n```\n![c](z.png)\n```\n![d](images/d.png)')).toEqual({ links: ['x.md'], images: ['images/d.png'] });
  });
});

describe('docs/user content', () => {
  // 与构建同一套收集规则, 校验的正是会打包进前端的内容
  const prefix = '../../../../docs/user/';
  const strip = (key: string) => key.slice(prefix.length);
  const raw = import.meta.glob('../../../../docs/user/**/*.md', { query: '?raw', import: 'default', eager: true }) as Record<string, string>;
  const everything = Object.keys(import.meta.glob('../../../../docs/user/**/*', { query: '?url', import: 'default', eager: true })).map(strip);
  const markdown = Object.keys(raw).map(strip);
  const assets = new Set(everything.filter((file) => !file.endsWith('.md')));

  it('has an index page', () => {
    expect(markdown).toContain('index.md');
  });

  it('only holds markdown and images', () => {
    expect(everything.filter((file) => !/\.(md|png|jpe?g|gif|webp|svg)$/i.test(file))).toEqual([]);
  });

  it('parses every page and resolves every link and image inside docs/user', () => {
    const pages = buildPages(Object.entries(raw).map(([key, value]) => ({ path: strip(key), raw: value })));
    const slugs = new Set(pages.map((page) => page.slug));
    const problems: string[] = [];
    for (const page of pages) {
      const { links, images } = extractReferences(page.body);
      for (const href of links) {
        const target = resolveLink(page.path, href, slugs);
        if (target.kind === 'invalid') problems.push(`${page.path}: ${target.reason}`);
      }
      for (const src of images) {
        const resolved = resolveImage(page.path, src);
        if (!resolved || !assets.has(resolved)) problems.push(`${page.path}: 图片不存在或越出 docs/user: ${src}`);
      }
    }
    expect(problems).toEqual([]);
  });
});
