import { rpc } from './transport.js';

export function getSetup() {
  return rpc('setup.get');
}

export function updateSetup(step) {
  return rpc('setup.update', { step });
}

export function finishSetup() {
  return rpc('setup.finish');
}

export function getManagedService(id = 'searxng') {
  return rpc('managed_service.get', { id });
}

export function applyManagedService(action, options = {}) {
  return rpc('managed_service.apply', { id: 'searxng', action, ...options });
}
