// 用户文档的纯逻辑: frontmatter, 页面标识, 相对链接与图片解析; 不依赖 DOM, 供页面和校验测试共用

/** 所有前端可见文档都在 docs/user 下, 路径均相对该目录 */
export interface DocSource {
  /** 相对 docs/user 的路径, 如 index.md, guide/plugins.md */
  path: string;
  raw: string;
}

export interface DocPage {
  /** URL 段, 如 index, guide/plugins */
  slug: string;
  path: string;
  title: string;
  order: number;
  body: string;
}

export interface DocFrontmatter {
  title?: string;
  order?: number;
}

const FRONTMATTER = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/;

/**
 * 解析开头的 frontmatter, 只接受 title 与 order 两个字段
 *
 * 格式错误时抛出, 由校验测试暴露而不是在页面上静默忽略
 */
export function parseFrontmatter(raw: string): { meta: DocFrontmatter; body: string } {
  const match = raw.match(FRONTMATTER);
  if (!match) return { meta: {}, body: raw };
  const meta: DocFrontmatter = {};
  for (const line of match[1].split(/\r?\n/)) {
    if (!line.trim()) continue;
    const field = line.match(/^(\w+):\s*(.*)$/);
    if (!field) throw new Error(`frontmatter 行无法解析: ${line}`);
    const [, key, rawValue] = field;
    const value = rawValue.trim().replace(/^(['"])(.*)\1$/, '$2');
    if (key === 'title') meta.title = value;
    else if (key === 'order') {
      const order = Number(value);
      if (!Number.isFinite(order)) throw new Error(`order 必须是数字: ${value}`);
      meta.order = order;
    } else throw new Error(`不支持的 frontmatter 字段: ${key}`);
  }
  return { meta, body: raw.slice(match[0].length) };
}

export function slugOf(path: string): string {
  return path.replace(/\.md$/i, '');
}

function firstHeading(body: string): string | null {
  const match = body.match(/^#\s+(.+?)\s*#*\s*$/m);
  return match ? match[1].trim() : null;
}

/** 构建页面列表: order 升序, 其次 index 优先, 再按路径 */
export function buildPages(sources: DocSource[]): DocPage[] {
  return sources.map((source) => {
    const { meta, body } = parseFrontmatter(source.raw);
    const slug = slugOf(source.path);
    const title = meta.title || firstHeading(body) || slug;
    return {
      slug,
      path: source.path,
      title,
      order: meta.order ?? (slug === 'index' ? 0 : 1000),
      // 页头已显示标题, 正文开头同名的一级标题不再重复
      body: body.replace(/^\s*#\s+(.+?)\s*#*\s*(\r?\n|$)/, (line, text: string) => (text.trim() === title ? '' : line)),
    };
  }).sort((a, b) => a.order - b.order || Number(b.slug === 'index') - Number(a.slug === 'index') || a.path.localeCompare(b.path));
}

/** 与标题渲染一致的锚点: 保留中文与字母数字, 空白转连字符 */
export function headingId(text: string): string {
  return text.trim().toLowerCase()
    .replace(/[^\p{L}\p{N}\s-]/gu, '')
    .replace(/\s+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '');
}

/** 文档目录内的路径拼接, 越出 docs/user 时返回 null */
export function resolveInside(fromPath: string, target: string): string | null {
  const base = fromPath.split('/').slice(0, -1);
  const parts = [...base];
  for (const segment of target.split('/')) {
    if (!segment || segment === '.') continue;
    if (segment === '..') {
      if (!parts.length) return null;
      parts.pop();
    } else parts.push(segment);
  }
  return parts.join('/');
}

export type LinkTarget =
  | { kind: 'external'; href: string }
  | { kind: 'anchor'; hash: string }
  | { kind: 'page'; slug: string; hash: string }
  | { kind: 'invalid'; reason: string };

const EXTERNAL = /^(https?:|mailto:)/i;

/** 把 md 里的链接分类; 页面链接只认 docs/user 中存在的 md */
export function resolveLink(fromPath: string, href: string, slugs: ReadonlySet<string>): LinkTarget {
  if (EXTERNAL.test(href)) return { kind: 'external', href };
  if (href.startsWith('#')) return { kind: 'anchor', hash: decodeURIComponent(href.slice(1)) };
  if (/^[a-z][a-z0-9+.-]*:/i.test(href) || href.startsWith('/')) return { kind: 'invalid', reason: `不支持的链接: ${href}` };
  const [pathPart, hashPart = ''] = href.split('#');
  if (!/\.md$/i.test(pathPart)) return { kind: 'invalid', reason: `只能链接到 md 页面: ${href}` };
  const resolved = resolveInside(fromPath, decodeURI(pathPart));
  if (resolved === null) return { kind: 'invalid', reason: `链接越出 docs/user: ${href}` };
  const slug = slugOf(resolved);
  if (!slugs.has(slug)) return { kind: 'invalid', reason: `链接的页面不存在: ${href}` };
  return { kind: 'page', slug, hash: decodeURIComponent(hashPart) };
}

/** 图片相对 md 所在目录解析, 返回 docs/user 内的路径或 null */
export function resolveImage(fromPath: string, src: string): string | null {
  if (EXTERNAL.test(src) || src.startsWith('/') || /^[a-z][a-z0-9+.-]*:/i.test(src)) return null;
  return resolveInside(fromPath, decodeURI(src.split(/[?#]/)[0]));
}

export interface OutlineItem {
  id: string;
  text: string;
  level: 2 | 3;
  children: OutlineItem[];
}

/** 去掉标题里的行内 md 记号, 与渲染后的文字保持一致, 锚点才对得上 */
function plainHeading(text: string): string {
  return text
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/`([^`]*)`/g, '$1')
    .replace(/(\*\*|__|\*|_|~~)(.+?)\1/g, '$2')
    .trim();
}

/** 由正文的二、三级标题构建大纲; 跳过代码块, 三级标题挂在最近的二级标题下 */
export function buildOutline(body: string): OutlineItem[] {
  const outline: OutlineItem[] = [];
  let fence: string | null = null;
  for (const line of body.split(/\r?\n/)) {
    const marker = line.match(/^\s*(```|~~~)/);
    if (marker) { fence = fence === marker[1] ? null : fence ?? marker[1]; continue; }
    if (fence) continue;
    const match = line.match(/^(#{2,3})\s+(.+?)\s*#*\s*$/);
    if (!match) continue;
    const text = plainHeading(match[2]);
    const item: OutlineItem = { id: headingId(text), text, level: match[1].length as 2 | 3, children: [] };
    const parent = outline[outline.length - 1];
    if (item.level === 3 && parent) parent.children.push(item);
    else outline.push({ ...item, level: 2 });
  }
  return outline;
}

/** 提取 md 中的链接与图片, 供校验; 忽略代码块和行内代码 */
export function extractReferences(body: string): { links: string[]; images: string[] } {
  const text = body.replace(/```[\s\S]*?```/g, '').replace(/`[^`\n]*`/g, '');
  const images = [...text.matchAll(/!\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)/g)].map((match) => match[1]);
  const links = [...text.matchAll(/(?<!!)\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)/g)].map((match) => match[1]);
  return { links, images };
}
