// A Session's timeline items live outside the Chat state's deep `$state`
// proxy. Server patches replace every changed Run, child and list and keep the
// unchanged ones (`chatTimeline.js`), so object identity alone tells what
// changed. Behind the proxy, every read of the thousands of rows and children
// a long Session holds would create and track a signal on each streaming
// update, and identity would never settle for derived projections.
//
// `timelineItems` becomes an accessor over one `$state.raw` signal; the proxy
// leaves accessors alone, so readers stay reactive to every reassignment.
// Writers therefore replace the array and never mutate it or its rows in
// place. Define it before the Session object enters the proxied state.
export function withRawTimelineItems(sessionState) {
  let items = $state.raw(sessionState.timelineItems ?? []);
  Object.defineProperty(sessionState, 'timelineItems', {
    configurable: true,
    enumerable: true,
    get: () => items,
    set: (value) => {
      items = value;
    },
  });
  return sessionState;
}
