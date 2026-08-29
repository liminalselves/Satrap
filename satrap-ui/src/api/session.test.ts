import { afterEach, describe, expect, it, vi } from 'vitest';

import { apiClient } from './client';
import { controlApi } from './control';
import { sessionApi } from './session';

describe('sessionApi cold management', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('uses the control service to discover session classes', async () => {
    const result = { paths: ['.satrap/session'], results: [] };
    const controlSpy = vi.spyOn(controlApi, 'discoverSessionClasses').mockResolvedValue(result);
    const backendSpy = vi.spyOn(apiClient, 'get');

    await expect(sessionApi.discover('.satrap/session')).resolves.toEqual(result);
    expect(controlSpy).toHaveBeenCalledWith('.satrap/session');
    expect(backendSpy).not.toHaveBeenCalled();
  });

  it('uses the control service to create the session scan directory', async () => {
    const result = { ok: true, path: '.satrap/session' };
    const controlSpy = vi.spyOn(controlApi, 'createSessionScanDirectory').mockResolvedValue(result);
    const backendSpy = vi.spyOn(apiClient, 'post');

    await expect(sessionApi.createScanDirectory('.satrap/session')).resolves.toEqual(result);
    expect(controlSpy).toHaveBeenCalledWith('.satrap/session');
    expect(backendSpy).not.toHaveBeenCalled();
  });

  it('deletes one runtime session through the backend API', async () => {
    const result = { ok: true, deleted_count: 1, deleted_ids: ['session/1'] };
    const backendSpy = vi.spyOn(apiClient, 'delete').mockResolvedValue(result);

    await expect(sessionApi.deleteRuntime('onebot-main', 'session/1')).resolves.toEqual(result);
    expect(backendSpy).toHaveBeenCalledWith('/api/sessions/session%2F1?platform_id=onebot-main');
  });

  it('passes custom runtime selection to the bulk delete endpoint', async () => {
    const result = { ok: true, deleted_count: 2, deleted_ids: ['one', 'two'] };
    const backendSpy = vi.spyOn(apiClient, 'post').mockResolvedValue(result);

    await expect(sessionApi.bulkDeleteRuntime({
      mode: 'selected',
      session_refs: [
        { platform_id: 'onebot-main', session_id: 'one' },
        { platform_id: 'misskey-main', session_id: 'two' },
      ],
    })).resolves.toEqual(result);
    expect(backendSpy).toHaveBeenCalledWith('/api/sessions/bulk-delete', {
      mode: 'selected',
      session_refs: [
        { platform_id: 'onebot-main', session_id: 'one' },
        { platform_id: 'misskey-main', session_id: 'two' },
      ],
    });
  });

  it('restarts one runtime session without replacing its cold config', async () => {
    const result = { ok: true, runtime: { plugins: [] } };
    const backendSpy = vi.spyOn(apiClient, 'post').mockResolvedValue(result);

    await expect(sessionApi.restartRuntime('onebot-platform', 'session/1')).resolves.toEqual(result);
    expect(backendSpy).toHaveBeenCalledWith(
      '/api/sessions/session%2F1/restart?platform_id=onebot-platform',
    );
  });

  it('uses cold instance APIs while the backend is stopped', async () => {
    const listed = { sessions: [] };
    const created = {
      ok: true,
      session: {
        platform_id: 'local',
        session_id: 'cold-1',
        created_at: 1,
        last_used_at: 1,
        message_count: 0,
        active: false,
      },
    };
    const deleted = { ok: true, deleted_count: 1, deleted_ids: ['cold-1'] };
    const listSpy = vi.spyOn(controlApi, 'listSessionInstances').mockResolvedValue(listed);
    const createSpy = vi.spyOn(controlApi, 'createSessionInstance').mockResolvedValue(created);
    const deleteSpy = vi.spyOn(controlApi, 'deleteSessionInstance').mockResolvedValue(deleted);

    await expect(sessionApi.listRuntime(false)).resolves.toEqual(listed);
    await expect(sessionApi.createRuntime({
      session_provider: 'edictum',
      session_type: 'assistant',
    }, false)).resolves.toEqual(created);
    await expect(sessionApi.deleteRuntime('local', 'cold-1', false)).resolves.toEqual(deleted);

    expect(listSpy).toHaveBeenCalledOnce();
    expect(createSpy).toHaveBeenCalledWith({
      session_provider: 'edictum',
      session_type: 'assistant',
    });
    expect(deleteSpy).toHaveBeenCalledWith('local', 'cold-1');
  });
});
