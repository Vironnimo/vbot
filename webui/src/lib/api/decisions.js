import { rpc } from './transport.js';

export const listDecisionExperiments = (options) =>
  rpc('decision.list', {}, options);
export const getDecisionExperiment = (id, options) =>
  rpc('decision.get', { id }, options);
export const saveDecisionExperiment = (draft, id, revision, options) =>
  rpc('decision.save', { draft, ...(id ? { id, revision } : {}) }, options);
export const deleteDecisionExperiment = (id, revision, options) =>
  rpc('decision.delete', { id, revision }, options);
export const getDecisionHistory = (id, before, options) =>
  rpc('decision.history', { id, ...(before ? { before } : {}) }, options);
export const startDecisionEvaluation = (
  id,
  revision,
  requestId,
  mode = 'evaluate',
  options,
) =>
  rpc('decision.start', { id, revision, request_id: requestId, mode }, options);
export const getDecisionResult = (id, options) =>
  rpc('decision.result', { id }, options);
export const cancelDecisionEvaluation = (id, options) =>
  rpc('decision.cancel', { id }, options);
export const evaluateDecision = (state, questions, options) =>
  rpc('decision.evaluate', { state, questions }, options);
