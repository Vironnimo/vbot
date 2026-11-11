import { rpc } from './transport.js';

export const listExternalSources = (options = {}) =>
  rpc('external.sources', {}, options);
export const listExternalSessions = (params, options = {}) =>
  rpc('external.list', params, options);
export const readExternalSession = (params, options = {}) =>
  rpc('external.show', params, options);
export const readExternalMemories = (params, options = {}) =>
  rpc('external.memories', params, options);
export const importExternalSession = (params, options = {}) =>
  rpc('external.import_session', params, options);
export const importExternalMemory = (params, options = {}) =>
  rpc('memory.import', params, options);
