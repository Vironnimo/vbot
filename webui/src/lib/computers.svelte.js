import { getContext, setContext } from 'svelte';
import { listComputers } from './api.js';
import { t } from './i18n.js';

const COMPUTERS_CONTEXT = Symbol('computers');

export function normalizeExecution(value) {
  const computerId =
    typeof value?.computer_id === 'string' && value.computer_id
      ? value.computer_id
      : 'server';
  const ids = [
    computerId,
    ...(Array.isArray(value?.allowed_computer_ids)
      ? value.allowed_computer_ids.filter((id) => typeof id === 'string' && id)
      : []),
  ];
  return {
    computer_id: computerId,
    allowed_computer_ids: ids.filter((id, index) => ids.indexOf(id) === index),
  };
}

export function computerName(computer) {
  return (
    computer?.name ||
    (computer?.id === 'server' ? t('computers.server') : computer?.id || '')
  );
}

export function computerDescription(computer) {
  if (!computer?.known) return t('computers.availabilityUnknown');
  const capabilities = computer.capabilities ?? [];
  let description;
  if (
    capabilities.includes('terminal') &&
    capabilities.includes('computer_use')
  ) {
    description = t('computers.terminalAndComputerUse');
  } else if (capabilities.includes('terminal')) {
    description = t('computers.terminalOnly');
  } else if (capabilities.includes('computer_use')) {
    description = t('computers.computerUseOnly');
  } else {
    description = t('computers.noCapabilities');
  }
  return computer.online
    ? description
    : t('computers.offlineCapabilities', { capabilities: description });
}

// A selected computer remains present even when it is absent from the latest
// catalog. A failed read never changes a Session's chosen execution target.
export function computersWithSelection(computers, ids = []) {
  const result = [...computers];
  for (const id of ['server', ...ids]) {
    if (!result.some((computer) => computer.id === id)) {
      result.push({
        id,
        name: '',
        known: false,
        online: false,
        capabilities: [],
      });
    }
  }
  return result;
}

export function computerOptions(computers, selected = [], capability = null) {
  return computersWithSelection(computers, selected).map((computer) => ({
    value: computer.id,
    label: computerName(computer),
    secondaryLabel: computerDescription(computer),
    disabled: Boolean(
      capability &&
      computer.known &&
      !computer.capabilities.includes(capability),
    ),
  }));
}

export function createComputers({ read = listComputers } = {}) {
  let computers = $state([]);
  let loaded = $state(false);
  let error = $state('');
  let reading = $state(false);
  let disposed = false;
  let pending = null;
  let again = false;

  function refresh() {
    if (disposed) return Promise.resolve();
    if (pending) {
      again = true;
      return pending;
    }
    pending = drain().finally(() => {
      pending = null;
    });
    return pending;
  }

  async function drain() {
    reading = true;
    try {
      do {
        again = false;
        try {
          const result = await read();
          if (disposed) return;
          computers = (Array.isArray(result?.computers) ? result.computers : [])
            .filter(
              (computer) => typeof computer?.id === 'string' && computer.id,
            )
            .map((computer) => ({
              ...computer,
              known: true,
              online: computer.online === true,
              capabilities: Array.isArray(computer.capabilities)
                ? computer.capabilities
                : [],
            }));
          loaded = true;
          error = '';
        } catch {
          if (!disposed) error = t('computers.loadError');
        }
      } while (again && !disposed);
    } finally {
      reading = false;
    }
  }

  return {
    get computers() {
      return computers;
    },
    get loaded() {
      return loaded;
    },
    get error() {
      return error;
    },
    get reading() {
      return reading;
    },
    refresh,
    destroy() {
      disposed = true;
    },
  };
}

export function provideComputers(catalog) {
  setContext(COMPUTERS_CONTEXT, catalog);
  return catalog;
}

export function useComputers() {
  return getContext(COMPUTERS_CONTEXT) ?? null;
}
