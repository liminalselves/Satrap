import { isValidElement, memo, useMemo, type ComponentPropsWithoutRef, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import ReactMarkdown, { type Components } from 'react-markdown';
import rehypeKatex from 'rehype-katex';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import { ImageOff } from 'lucide-react';
import { CopyButton } from '@/components/ui/CopyButton';
import { headingId, resolveImage, resolveLink } from './docModel';
import 'katex/dist/katex.min.css';

const REMARK_PLUGINS = [remarkGfm, remarkMath];
const REHYPE_PLUGINS = [rehypeKatex];

function textOf(node: ReactNode): string {
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(textOf).join('');
  if (isValidElement<{ children?: ReactNode }>(node)) return textOf(node.props.children);
  return '';
}

function CodeBlock({ children, ...props }: ComponentPropsWithoutRef<'pre'>) {
  return (
    <div className="markdown-code-block">
      <CopyButton text={textOf(children).replace(/\n$/, '')} title="复制代码" className="markdown-code-copy p-1.5" />
      <pre {...props}>{children}</pre>
    </div>
  );
}

function heading(Tag: 'h1' | 'h2' | 'h3' | 'h4') {
  return function Heading({ children }: ComponentPropsWithoutRef<'h1'>) {
    const id = headingId(textOf(children));
    return (
      <Tag id={id} className="group scroll-mt-24">
        {children}
        {Tag !== 'h1' && <a href={`#${id}`} aria-label="本节链接" className="ml-2 !no-underline opacity-0 transition-opacity group-hover:opacity-60 focus-visible:opacity-60">#</a>}
      </Tag>
    );
  };
}

/** 渲染 docs/user 中的一页; 相对链接在文档页内跳转, 图片换成构建后的资源地址 */
export const DocMarkdown = memo(function DocMarkdown({ path, body, slugs, images }: {
  path: string; body: string; slugs: ReadonlySet<string>; images: ReadonlyMap<string, string>;
}) {
  const components = useMemo<Components>(() => ({
    pre: CodeBlock,
    h1: heading('h1'), h2: heading('h2'), h3: heading('h3'), h4: heading('h4'),
    a({ href = '', children }) {
      const target = resolveLink(path, href, slugs);
      if (target.kind === 'external') return <a href={target.href} target="_blank" rel="noopener noreferrer">{children}</a>;
      if (target.kind === 'anchor') return <a href={`#${encodeURIComponent(target.hash)}`}>{children}</a>;
      if (target.kind === 'page') return <Link to={`/docs/${target.slug}${target.hash ? `#${encodeURIComponent(target.hash)}` : ''}`}>{children}</Link>;
      return <span title={target.reason}>{children}</span>;
    },
    img({ src = '', alt = '' }) {
      const resolved = typeof src === 'string' ? resolveImage(path, src) : null;
      const url = resolved ? images.get(resolved) : undefined;
      if (!url) {
        return (
          <span className="my-3 flex items-center gap-2 rounded-lg border border-dashed border-line px-3 py-2 text-xs text-fg-4">
            <ImageOff className="h-3.5 w-3.5" />图片不可用{alt ? `: ${alt}` : ''}
          </span>
        );
      }
      return <img src={url} alt={alt} loading="lazy" className="my-3 max-w-full rounded-xl border border-line" />;
    },
  }), [path, slugs, images]);

  return (
    <div className="markdown-body doc-body">
      {/* 保留默认 urlTransform 过滤危险协议; 相对路径不受影响, 由上面的组件再映射 */}
      <ReactMarkdown remarkPlugins={REMARK_PLUGINS} rehypePlugins={REHYPE_PLUGINS} components={components}>
        {body}
      </ReactMarkdown>
    </div>
  );
});
