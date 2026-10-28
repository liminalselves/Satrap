import { useEffect, useMemo } from 'react';
import { Link, Navigate, useLocation, useParams } from 'react-router-dom';
import { ArrowLeft, BookOpen, FileQuestion } from 'lucide-react';
import { Card, EmptyState, PageHeader } from '@/components/kit';
import { usePrefsStore } from '@/stores/usePrefsStore';
import { docImages, docPages } from './docSources';
import { DocMarkdown } from './DocMarkdown';
import { DocOutline } from './DocOutline';

const slugs = new Set(docPages.map((page) => page.slug));

/** 用户文档页: /docs 与 /docs/<页面>#<标题>, 内容来自构建时打包的 docs/user */
export function Docs() {
  const params = useParams();
  const { hash } = useLocation();
  const motion = usePrefsStore((state) => state.motion);
  const slug = params['*'] || 'index';
  const page = useMemo(() => docPages.find((item) => item.slug === slug), [slug]);

  // 渲染后再定位: 深链接的锚点在页面内容出现前不存在
  useEffect(() => {
    const target = hash ? document.getElementById(decodeURIComponent(hash.slice(1))) : null;
    if (target) target.scrollIntoView({ behavior: motion === 'full' ? 'smooth' : 'auto', block: 'start' });
    else document.querySelector('main')?.scrollTo({ top: 0 });
  }, [slug, hash, motion]);

  if (!docPages.length) {
    return <EmptyState icon={<BookOpen className="h-6 w-6" />} title="暂无文档" description="docs/user 下还没有文档" />;
  }
  if (!page) {
    if (slug === 'index') return <Navigate replace to={`/docs/${docPages[0].slug}`} />;
    return (
      <EmptyState icon={<FileQuestion className="h-6 w-6" />} title="文档不存在" description={`没有名为 ${slug} 的文档页`}
        actions={<Link to="/docs" className="text-sm text-accent hover:underline">返回文档首页</Link>} />
    );
  }

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow={<Link to="/settings" className="inline-flex items-center gap-1 text-xs text-fg-3 transition hover:text-fg"><ArrowLeft className="h-3 w-3" />系统设置</Link>}
        title={page.title}
        description="使用说明, 随当前版本一起发布"
      />
      <div className="grid gap-5 lg:grid-cols-[240px_minmax(0,1fr)]">
        <aside className="lg:sticky lg:top-0 lg:max-h-[calc(100vh-7rem)] lg:self-start lg:overflow-y-auto">
          <Card padding="sm">
            <DocOutline pages={docPages} current={page} />
          </Card>
        </aside>
        <Card padding="lg" className="min-w-0">
          <article className="mx-auto max-w-3xl">
            <DocMarkdown key={page.slug} path={page.path} body={page.body} slugs={slugs} images={docImages} />
          </article>
        </Card>
      </div>
    </div>
  );
}
