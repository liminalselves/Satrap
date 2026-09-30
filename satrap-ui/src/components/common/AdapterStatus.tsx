import { Badge } from '@/components/ui/Badge';
import type { AdapterInfo, PlatformConfigApplication } from '@/api/types';
import { useConfigStore } from '@/stores/useConfigStore';

export function AdapterStatus({ info, application }: { info: AdapterInfo; application?: PlatformConfigApplication }) {
  const { sessionClasses, edictumConfigs } = useConfigStore();
  const sessionEnabled = (info.session_provider === 'edictum' ? edictumConfigs : sessionClasses)[info.session_type || '']?.enabled;
  return <div className="flex flex-wrap items-center gap-2">
    <Badge variant={info.status === 'running' ? 'success' : info.status === 'error' ? 'error' : 'warning'}>
      平台: {info.status}
    </Badge>
    <Badge variant={info.started ? 'success' : 'default'}>{info.started ? '适配器已启动' : '适配器未启动'}</Badge>
    <Badge variant={sessionEnabled === false ? 'warning' : 'default'}>
      {sessionEnabled === undefined ? '会话配置未知' : sessionEnabled ? '会话配置启用' : '会话配置停用'}
    </Badge>
    {application && <Badge variant={application.status === 'applied' ? 'success' : application.status === 'failed' ? 'error' : 'warning'}>
      {{ applied: '配置已应用', failed: '配置应用失败', pending_restart: '配置待重启' }[application.status]}
    </Badge>}
  </div>;
}
