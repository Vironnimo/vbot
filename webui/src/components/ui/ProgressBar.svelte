<script>
  // A thin determinate progress bar with its value as text beneath it,
  // such as "1.2 GB of 4.1 GB".
  let { label, percent, text = '' } = $props();
</script>

<div
  class="progress-bar"
  role="progressbar"
  aria-label={label}
  aria-valuemin="0"
  aria-valuemax="100"
  aria-valuenow={percent}
  aria-valuetext={text || undefined}
>
  <span style:width={`${percent}%`}></span>
</div>
{#if text}
  <span class="progress-bar__text">{text}</span>
{/if}

<style>
  .progress-bar {
    height: 4px;
    overflow: hidden;
    border-radius: 2px;
    background: var(--border);
  }

  .progress-bar > span {
    display: block;
    height: 100%;
    background: var(--accent);
    transition: width 0.4s ease;
  }

  .progress-bar__text {
    color: var(--text-lo);
    font-size: var(--fs-label-sm);
    font-variant-numeric: tabular-nums;
  }
</style>
