import { rpc } from './transport.js';

// Send one batch of browser measurements (see lib/clientMetrics.js).
export function reportClientMetrics(report, options = {}) {
  return rpc('performance.client_report', report, {
    ...options,
    untracked: true,
  });
}
