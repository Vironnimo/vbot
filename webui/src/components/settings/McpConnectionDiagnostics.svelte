<script>
  // What a failed connection's details show about why it does not work: the
  // technical error and the last lines a local server wrote to its error
  // output. The advice for the diagnosed problem and the credentials without
  // a value stand in the connection's row.
  import { t } from '$lib/i18n.js';

  let { connection } = $props();

  let tail = $derived(connection.stderr_tail ?? []);
</script>

{#if connection.state === 'failed' && connection.problem && connection.error}
  <details class="mcp-diagnostics">
    <summary>{t('mcp.technicalDetails')}</summary>
    <p class="mcp-guidance">{connection.error}</p>
  </details>
{/if}
{#if connection.state === 'failed' && tail.length}
  <details class="mcp-diagnostics" open>
    <summary>{t('mcp.serverOutput', { count: tail.length })}</summary>
    <pre class="mcp-diagnostics__output">{tail.join('\n')}</pre>
  </details>
{/if}
