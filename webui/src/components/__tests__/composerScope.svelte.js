export function createComposerScope(initialKey) {
  let draftKey = $state(initialKey);
  return {
    get draftKey() {
      return draftKey;
    },
    set draftKey(value) {
      draftKey = value;
    },
  };
}
