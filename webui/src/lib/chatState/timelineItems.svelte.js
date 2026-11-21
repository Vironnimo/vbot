// History records are replaced as snapshots, including every changed Timeline
// child. Track assignments to their collections without proxying every field.
export function withRawHistory(sessionState) {
  for (const key of ['messages', 'timelineItems', 'historyRuns', 'runEvents']) {
    let records = $state.raw(sessionState[key]);
    Object.defineProperty(sessionState, key, {
      configurable: true,
      enumerable: true,
      get: () => records,
      set: (value) => {
        records = value;
      },
    });
  }
  return sessionState;
}
