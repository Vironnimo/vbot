<script>
  import { tooltip } from '$lib/tooltip.js';

  // The end of a collapsed Tool row: how the call stands, in words. A call
  // that went well shows only its duration in the quiet row color; red,
  // amber or grey words mark only what needs attention (failed, partial,
  // refused, interrupted, cancelled), and a running call shows a spinner
  // before its ticking time. There is no leading status mark (user decision
  // 2026-11-20): the row's state lives here, so it never rests on color
  // alone. `status` is the outcome that colors the words; `running` says
  // whether anything of the call still runs, which a background command
  // whose shell already exited with an outcome can (spinner and outcome
  // together). A call the Model is still writing shows nothing yet.
  // `details` feeds the shared tooltip with the state, start and runtime.
  let {
    status = '',
    label = '',
    details = null,
    running = undefined,
  } = $props();

  const spinning = $derived(running ?? status === 'running');
</script>

{#if label || spinning}
  <span
    class="te-time"
    class:failed={status === 'failed'}
    class:partial={status === 'partial'}
    class:refused={status === 'refused'}
    class:interrupted={status === 'interrupted'}
    class:cancelled={status === 'cancelled'}
    class:running={spinning}
    data-status={status || undefined}
    use:tooltip={details}
    >{#if spinning}<svg
        class="te-spinner"
        viewBox="0 0 16 16"
        width="11"
        height="11"
        aria-hidden="true"><path d="M8 2a6 6 0 1 0 6 6" /></svg
      >{/if}{label}</span
  >
{/if}
