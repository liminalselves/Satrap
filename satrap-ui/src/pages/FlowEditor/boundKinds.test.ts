import { describe, expect, it } from 'vitest';
import type { PlatformConfig } from '@/api/types';
import { boundKinds } from './index';

const platform = (patch: Partial<PlatformConfig>): PlatformConfig => ({ id: 'p', type: 'onebot', settings: {}, ...patch });

describe('boundKinds', () => {
  it('uses conversation-kind bindings that name this config', () => {
    expect(boundKinds('rp', [platform({ session_bindings: { private: { mode: 'value', provider: 'edictum', config_name: 'rp' } } })])).toBe('private');
  });

  it('treats a platform default binding as both kinds unless a kind overrides it', () => {
    expect(boundKinds('rp', [platform({ session_provider: 'edictum', session_type: 'rp' })])).toBe('both');
    expect(boundKinds('rp', [platform({ session_provider: 'edictum', session_type: 'rp',
      session_bindings: { group: { mode: 'value', provider: 'edictum', config_name: 'other' } } })])).toBe('private');
  });

  it('falls back to group when no platform uses the config', () => {
    expect(boundKinds('rp', [platform({ session_provider: 'edictum', session_type: 'other' })])).toBe('group');
    expect(boundKinds('rp', [])).toBe('group');
  });
});
