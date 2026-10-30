// 构建时收集 docs/user 下的文档与图片; 新增文件下次构建自动出现, 无需改代码
import { buildPages, type DocPage } from './docModel';

const PREFIX = '../../../../docs/user/';

// 文本体积小, 直接打包进文档页 chunk, 目录与标题无需再请求
const markdown = import.meta.glob('../../../../docs/user/**/*.md', { query: '?raw', import: 'default', eager: true }) as Record<string, string>;
// 图片作为带哈希的静态资源, 只在页面渲染到时由浏览器加载
const images = import.meta.glob('../../../../docs/user/**/*.{png,jpg,jpeg,gif,webp,svg}', { query: '?url', import: 'default', eager: true }) as Record<string, string>;

const strip = (key: string) => key.slice(PREFIX.length);

export const docPages: DocPage[] = buildPages(Object.entries(markdown).map(([key, raw]) => ({ path: strip(key), raw })));

export const docImages: ReadonlyMap<string, string> = new Map(Object.entries(images).map(([key, url]) => [strip(key), url]));
