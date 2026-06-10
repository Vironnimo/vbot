// Small `.svelte.js` helper used by ChatView tests to mirror the parent
// App's reactive `connectionSnapshot` flow. App.svelte passes the `/ws`
// `connection_ready` hello frame down as a prop; the harness lets a test
// swap the snapshot reference reactively so ChatView's apply-once effect
// observes the change. `$state.raw` keeps the snapshot's object identity
// intact — ChatView's dedup compares by reference, and a deep proxy would
// get a fresh identity on re-assignment.
export function createChatViewConnectionSnapshotHarness() {
  let connectionSnapshot = $state.raw(null);
  return {
    get connectionSnapshot() {
      return connectionSnapshot;
    },
    setConnectionSnapshot(snapshot) {
      connectionSnapshot = snapshot;
    },
  };
}
