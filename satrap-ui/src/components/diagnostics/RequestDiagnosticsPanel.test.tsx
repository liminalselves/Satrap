import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { REJECTION_STAGE_FILTER, RequestDiagnosticsPanel, attachmentLabel, reasonLabel } from './RequestDiagnosticsPanel';

// 静态渲染只验证控件契约 (勾选/禁用/入口), 取数与筛选的实际行为由 Playwright 覆盖
function rejectionCheckbox(markup: string): string {
  const match = /<input[^>]*data-testid="diagnostics-rejections-only"[^>]*>/.exec(markup);
  if (!match) throw new Error('未找到"仅看拒绝"开关');
  return match[0];
}

describe('RequestDiagnosticsPanel', () => {
  it('仅看拒绝预设默认勾选, 平台页默认不勾选', () => {
    const preset = renderToStaticMarkup(<RequestDiagnosticsPanel rejectionsOnlyDefault />);
    expect(preset).toContain('仅看拒绝');
    expect(rejectionCheckbox(preset)).toContain('checked=""');
    const platformPage = renderToStaticMarkup(<RequestDiagnosticsPanel />);
    expect(rejectionCheckbox(platformPage)).not.toContain('checked=""');
  });

  it('聚焦请求后取消并禁用拒绝筛选, 提供返回近期请求入口', () => {
    const focused = renderToStaticMarkup(
      <RequestDiagnosticsPanel requestId="req-1" rejectionsOnlyDefault onClearRequest={vi.fn()} />,
    );
    expect(rejectionCheckbox(focused)).toContain('disabled=""');
    expect(rejectionCheckbox(focused)).not.toContain('checked=""');
    expect(focused).toContain('data-testid="diagnostics-clear-request"');
  });

  it('未提供清除入口时不渲染返回按钮 (平台页无聚焦请求)', () => {
    const focused = renderToStaticMarkup(<RequestDiagnosticsPanel requestId="req-1" />);
    expect(focused).not.toContain('diagnostics-clear-request');
  });

  it('拒绝预设的阶段取值与后端拒绝阶段一致', () => {
    expect(REJECTION_STAGE_FILTER).toBe('wake_decision,rate_limit');
  });

  it('图片与视频媒体诊断码有中文标签', () => {
    expect(attachmentLabel('image:failed:image_url_refreshed')).toBe('图片 失败 (图片地址已刷新)');
    expect(attachmentLabel('video:failed:video_unsupported_by_implementation')).toBe('视频 失败 (当前实现不支持视频回源)');
    expect(reasonLabel('image_unavailable')).toBe('图片无法获取');
    expect(reasonLabel('image_media_budget_dropped')).toBe('图片超出本事件媒体预算');
  });
});
