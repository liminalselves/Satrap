import { useCallback, useEffect, useRef, useState } from 'react';
import { RuntimeApplication, type ApplicationSnapshot } from '@/api/runtimeApplication';

interface ApplicationView<T> {
  application: RuntimeApplication<T>;
  kind: 'plugins' | 'full';
  snapshot: ApplicationSnapshot<T> | null;
  result: T | null;
  busy: boolean;
  error: string;
}

export function useRuntimeApplication<T>() {
  const [view, setView] = useState<ApplicationView<T> | null>(null);
  const current = useRef<ApplicationView<T> | null>(null);
  const lifetime = useRef(new AbortController());
  const busy = useRef(false);
  useEffect(() => {
    lifetime.current = new AbortController();
    return () => lifetime.current.abort();
  }, []);

  const update = useCallback((application: RuntimeApplication<T>, patch: Partial<ApplicationView<T>>) => {
    if (lifetime.current.signal.aborted || current.current?.application !== application) return;
    current.current = { ...current.current, ...patch };
    setView(current.current);
  }, []);

  const apply = useCallback(async (application: RuntimeApplication<T>, kind: 'plugins' | 'full') => {
    if (busy.current) throw new Error('请先等待本次应用结果或查询状态');
    busy.current = true;
    const signal = lifetime.current.signal;
    current.current = { application, kind, snapshot: null, result: null, busy: true, error: '' };
    setView(current.current);
    try {
      const result = await application.start((snapshot) => update(application, { snapshot }), signal);
      update(application, { result });
      return result;
    } catch (error) {
      update(application, { error: error instanceof Error ? error.message : '生效状态未确认' });
      throw error;
    } finally {
      busy.current = false;
      update(application, { busy: false });
    }
  }, [update]);

  const query = useCallback(async () => {
    const application = current.current?.application;
    if (!application || busy.current) return;
    busy.current = true;
    update(application, { busy: true, error: '' });
    const signal = lifetime.current.signal;
    try {
      const snapshot = await application.query(signal);
      const result = await application.observe(snapshot, (value) => update(application, { snapshot: value }), signal);
      update(application, { result });
    } catch (error) {
      update(application, { error: error instanceof Error ? error.message : '生效状态未确认' });
    } finally {
      busy.current = false;
      update(application, { busy: false });
    }
  }, [update]);

  const getSignal = useCallback(() => lifetime.current.signal, []);
  const clear = useCallback(() => {
    if (busy.current || lifetime.current.signal.aborted) return;
    current.current = null;
    setView(null);
  }, []);
  return { view, apply, query, getSignal, clear };
}
