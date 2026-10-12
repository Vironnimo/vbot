<script>
  // The newest output of one background command, shown in the Activity panel
  // while its output view is open. It reads the output when it mounts and
  // again every 2 seconds while the command or processes it started run.
  // Reading stops when the view closes or unmounts, and after the first read
  // that finds nothing of the command running, which shows its final output.
  // A failed read shows in place; while the command still runs, the next read
  // tries again, unless the server no longer knows the command.
  import { untrack } from 'svelte';

  import { boundedScroll } from '$lib/boundedScroll.js';
  import { t } from '$lib/i18n.js';

  const REFRESH_MS = 2000;
  const STATUS_RUNNING = 'running';

  let {
    id = undefined,
    commandId,
    // Whether the panel shows the command as running; a failed read is tried
    // again only then.
    running = false,
    // (commandId) => Promise of `{ ok: true, status, stillRunning, output }`
    // or `{ ok: false, error, gone }`.
    load,
    // The output box's accessible name.
    label = '',
  } = $props();

  // The command the view reads. Callers pass it from rows rebuilt on every
  // clock tick; only a changed id restarts the reads.
  let target = $derived(commandId);

  let output = $state('');
  let loaded = $state(false);
  let error = $state('');
  let gone = $state(false);

  $effect(() => {
    const readCommandId = target;
    const read = untrack(() => load);
    let disposed = false;
    let timer = null;

    const schedule = () => {
      timer = setTimeout(refresh, REFRESH_MS);
    };

    async function refresh() {
      timer = null;
      let result;
      try {
        result = await read(readCommandId);
      } catch {
        result = { ok: false, error: '', gone: false };
      }
      if (disposed) {
        return;
      }
      loaded = true;
      if (result?.ok) {
        output = typeof result.output === 'string' ? result.output : '';
        error = '';
        gone = false;
        if (result.status === STATUS_RUNNING || result.stillRunning === true) {
          schedule();
        }
        return;
      }
      gone = result?.gone === true;
      error = gone ? '' : (result?.error ?? '');
      if (!gone && running) {
        schedule();
      }
    }

    // The first read starts untracked: whatever the loader reads must not
    // restart this view.
    untrack(() => {
      output = '';
      loaded = false;
      error = '';
      gone = false;
      void refresh();
    });

    return () => {
      disposed = true;
      if (timer !== null) {
        clearTimeout(timer);
      }
    };
  });

  let failed = $derived(gone || Boolean(error));
</script>

<div {id} class="command-output">
  {#if !loaded}
    <p class="command-output__note">
      {t('chat.activity.commandOutput.loading')}
    </p>
  {:else if output}
    <div
      class="command-output__scroll"
      role="group"
      aria-label={label}
      use:boundedScroll={{ follow: true }}
    >
      <pre class="command-output__text">{output}</pre>
    </div>
  {:else if !failed}
    <p class="command-output__note">
      {t('chat.activity.commandOutput.empty')}
    </p>
  {/if}
  {#if gone}
    <p class="command-output__note">
      {t('chat.activity.commandOutput.gone')}
    </p>
  {:else if error}
    <p class="command-output__error" role="alert">
      {t('chat.activity.commandOutput.loadError', { message: error })}
    </p>
  {/if}
</div>

<style>
  .command-output {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: 6px;
    padding: 2px 8px 8px 24px;
  }
  .command-output__scroll {
    max-height: 220px;
    overflow: auto;
    border: 1px solid var(--border);
    border-radius: var(--r-sm);
    background: var(--bg);
    scrollbar-width: thin;
  }
  .command-output__scroll:focus-visible {
    outline: 1px solid var(--accent);
    outline-offset: -1px;
  }
  .command-output__text {
    margin: 0;
    padding: 6px 8px;
    color: var(--text-med);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    line-height: 1.5;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .command-output__note,
  .command-output__error {
    margin: 0;
    font-size: var(--fs-body-sm);
    line-height: 1.4;
  }
  .command-output__note {
    color: var(--text-med);
  }
  .command-output__error {
    color: var(--red);
    overflow-wrap: anywhere;
  }
</style>
