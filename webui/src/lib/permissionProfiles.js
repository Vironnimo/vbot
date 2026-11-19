import { readable } from 'svelte/store';
import { listPermissionProfiles } from './api.js';
import { t } from './i18n.js';

const initial = { profiles: [], loading: true, error: '' };
let refreshOpenCatalog;

// Only mounted pickers and the profile editor need this catalog. A refresh
// overtaking an earlier read is authoritative even if the earlier read ends last.
export const permissionProfiles = readable(initial, (set) => {
  let sequence = 0;
  let snapshot = initial;
  const publish = (next) => {
    snapshot = next;
    set(next);
  };
  refreshOpenCatalog = async () => {
    const ticket = ++sequence;
    publish({ ...snapshot, loading: true, error: '' });
    try {
      const { profiles } = await listPermissionProfiles();
      if (!Array.isArray(profiles))
        throw new Error(t('permissions.profile.loadError'));
      if (ticket === sequence) publish({ profiles, loading: false, error: '' });
    } catch (error) {
      if (ticket === sequence) {
        publish({
          ...snapshot,
          error: error?.message || String(error),
          loading: false,
        });
      }
    }
    return snapshot;
  };
  void refreshOpenCatalog();
  return () => {
    sequence++;
    refreshOpenCatalog = null;
  };
});

export function refreshPermissionProfiles() {
  return refreshOpenCatalog?.() ?? Promise.resolve(initial);
}

export function permissionProfileLabel(name) {
  if (name === 'full_access') return t('permissions.preset.full_access');
  if (name === 'restricted') return t('permissions.profile.restricted');
  return name;
}
