import { isValidElement, memo, type ComponentPropsWithoutRef, type ReactNode } from 'react';
import ReactMarkdown from 'react-markdown';
import rehypeKatex from 'rehype-katex';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import { CopyButton } from '@/components/ui/CopyButton';
import 'katex/dist/katex.min.css';

const MARKDOWN_REMARK_PLUGINS = [remarkGfm, remarkMath];
const MARKDOWN_REHYPE_PLUGINS = [rehypeKatex];

function extractNodeText(node: ReactNode): string {
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(extractNodeText).join('');
  if (isValidElement<{ children?: ReactNode }>(node)) return extractNodeText(node.props.children);
  return '';
}

function MarkdownCodeBlock({ children, ...props }: ComponentPropsWithoutRef<'pre'>) {
  const text = extractNodeText(children).replace(/\n$/, '');
  return (
    <div className="markdown-code-block">
      <CopyButton
        text={text}
        title="复制代码"
        className="markdown-code-copy p-1.5"
      />
      <pre {...props}>{children}</pre>
    </div>
  );
}

const MARKDOWN_COMPONENTS = {
  pre: MarkdownCodeBlock,
};

const MarkdownContent = memo(function MarkdownContent({ content }: { content: string }) {
  return (
    <div className="markdown-body">
      <ReactMarkdown
        remarkPlugins={MARKDOWN_REMARK_PLUGINS}
        rehypePlugins={MARKDOWN_REHYPE_PLUGINS}
        components={MARKDOWN_COMPONENTS}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
});

export default MarkdownContent;
