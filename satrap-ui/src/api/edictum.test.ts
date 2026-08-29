import { afterEach, describe, expect, it, vi } from 'vitest';

import { controlApi } from './control';
import { backendApi } from './backend';
import { edictumApi } from './edictum';

describe('edictumApi cold management', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('delegates type and config reads to the control service', async () => {
    const typesSpy = vi.spyOn(controlApi, 'listEdictumTypes').mockResolvedValue([]);
    const pluginsSpy = vi.spyOn(controlApi, 'listEdictumPlugins').mockResolvedValue([]);
    const listSpy = vi.spyOn(controlApi, 'listEdictumSessions').mockResolvedValue({});

    await expect(edictumApi.listTypes()).resolves.toEqual([]);
    await expect(edictumApi.listPlugins()).resolves.toEqual([]);
    await expect(edictumApi.list()).resolves.toEqual({});
    expect(typesSpy).toHaveBeenCalledOnce();
    expect(pluginsSpy).toHaveBeenCalledOnce();
    expect(listSpy).toHaveBeenCalledOnce();
  });

  it('delegates create, update, enable, disable and delete operations', async () => {
    const createSpy = vi.spyOn(controlApi, 'createEdictumSession').mockResolvedValue({ ok: true });
    const updateSpy = vi.spyOn(controlApi, 'updateEdictumSession').mockResolvedValue({ ok: true });
    const enabledSpy = vi.spyOn(controlApi, 'setEdictumSessionEnabled').mockResolvedValue({ ok: true });
    const deleteSpy = vi.spyOn(controlApi, 'deleteEdictumSession').mockResolvedValue({ ok: true });
    const payload = {
      name: 'assistant',
      edictum_type: 'async_simple',
      model_name: 'default',
      params: { stream: true },
      plugins: ['session_commands'],
    };

    await edictumApi.create(payload);
    await edictumApi.update('assistant', { ...payload, name: 'renamed' });
    await edictumApi.enable('renamed');
    await edictumApi.disable('renamed');
    await edictumApi.remove('renamed');

    expect(createSpy).toHaveBeenCalledWith(payload);
    expect(updateSpy).toHaveBeenCalledWith('assistant', { ...payload, name: 'renamed' });
    expect(enabledSpy).toHaveBeenNthCalledWith(1, 'renamed', true);
    expect(enabledSpy).toHaveBeenNthCalledWith(2, 'renamed', false);
    expect(deleteSpy).toHaveBeenCalledWith('renamed');
  });

  it('delegates active session plugin application to the running backend', async () => {
    const reloadSpy = vi.spyOn(backendApi, 'reloadConfig').mockResolvedValue({
      ok: true,
      edictum_sessions: [],
    });

    await expect(edictumApi.applyRuntimeChanges()).resolves.toEqual({
      ok: true,
      edictum_sessions: [],
    });
    expect(reloadSpy).toHaveBeenCalledOnce();
  });

  it('delegates plugin preview and targeted retry to the running backend', async () => {
    const previewSpy = vi.spyOn(backendApi, 'previewEdictumPlugins').mockResolvedValue({
      ok: true,
      edictum_sessions: [],
    });
    const retrySpy = vi.spyOn(backendApi, 'reconcileEdictumPlugins').mockResolvedValue({
      ok: true,
      edictum_sessions: [],
    });
    const plugins = [{ name: 'session_commands', enabled: false }];
    const refs = [{ platform_id: 'onebot-platform', session_id: 'session-1' }];

    await edictumApi.previewRuntimeChanges('onebot-edictum', plugins);
    await edictumApi.retryRuntimeChanges({ session_refs: refs });

    expect(previewSpy).toHaveBeenCalledWith({
      config_name: 'onebot-edictum',
      plugins,
    });
    expect(retrySpy).toHaveBeenCalledWith({ session_refs: refs });
  });
});
