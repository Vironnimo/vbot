<script>
  // Mounts an editor under an explicit autosave coordinator so tests can
  // inspect pending state and run the App's transition flush.
  import { provideAutosaveContext } from '../../lib/autosave.js';

  let { component: Editor, componentProps = {}, coordinator } = $props();

  provideAutosaveContext({
    register: (participant) => coordinator.register(participant),
    requestTransition: async (action) =>
      (await coordinator.flushPending()) ? action() : false,
  });
</script>

<Editor {...componentProps} />
