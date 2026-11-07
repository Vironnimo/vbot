<script>
  import { t } from '../../../../webui/src/lib/i18n.js';
  import { stepColumns, stepName } from './workflows.js';

  // A Workflow as a flow: steps left to right by what they come after,
  // arrows for `after`, dashed arrows below for sending work back. The setup
  // editor passes `onSelect` and gets buttons; a Card passes `status`.
  let {
    workflow,
    agentLabel = () => '',
    status = null,
    selectedId = '',
    onSelect = null,
  } = $props();

  const WIDTH = 184;
  const HEIGHT = 62;
  const COLUMN_GAP = 64;
  const ROW_GAP = 26;
  const PAD = 14;
  const LANE = 20;

  const steps = $derived(workflow?.steps ?? []);
  const layout = $derived.by(() => {
    const columns = stepColumns(steps);
    const rows = [];
    const place = {};
    for (const step of steps) {
      const column = columns.get(step.id);
      rows[column] = (rows[column] ?? 0) + 1;
      place[step.id] = {
        x: PAD + column * (WIDTH + COLUMN_GAP),
        y: PAD + 12 + (rows[column] - 1) * (HEIGHT + ROW_GAP),
      };
    }
    const width =
      PAD * 2 + Math.max(0, rows.length * (WIDTH + COLUMN_GAP) - COLUMN_GAP);
    const bottom =
      PAD + 12 + Math.max(1, ...rows) * (HEIGHT + ROW_GAP) - ROW_GAP;
    const back = steps.filter(
      (step) => step.returns_to && place[step.returns_to],
    );
    return {
      place,
      width,
      bottom,
      height: bottom + PAD + back.length * LANE + (back.length ? 8 : 0),
      back,
    };
  });

  function forward(from, to) {
    const a = layout.place[from];
    const b = layout.place[to];
    const x1 = a.x + WIDTH;
    const y1 = a.y + HEIGHT / 2;
    const x2 = b.x - 6;
    const y2 = b.y + HEIGHT / 2;
    const bend = (x2 - x1) / 2;
    return `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`;
  }

  function backward(step, index) {
    const a = layout.place[step.id];
    const b = layout.place[step.returns_to];
    const level = layout.bottom + 16 + index * LANE;
    const x1 = a.x + WIDTH / 2 + 10;
    const x2 = b.x + WIDTH / 2 - 10;
    return {
      path: `M ${x1} ${a.y + HEIGHT} V ${level} H ${x2} V ${b.y + HEIGHT + 6}`,
      label: { x: (x1 + x2) / 2, y: level - 5 },
    };
  }

  function statusLabel(value) {
    switch (value) {
      case 'done':
        return t('factory.stepStatus.done');
      case 'current':
        return t('factory.stepStatus.current');
      case 'waiting':
        return t('factory.stepStatus.waiting');
      default:
        return t('factory.stepStatus.pending');
    }
  }
</script>

<div class="factory-diagram" data-workflow-diagram={workflow?.id}>
  <div
    class="factory-diagram-canvas"
    style:width={`${layout.width}px`}
    style:height={`${layout.height}px`}
  >
    <svg
      width={layout.width}
      height={layout.height}
      aria-hidden="true"
      focusable="false"
    >
      <defs>
        <marker
          id="factory-arrow"
          viewBox="0 0 10 10"
          refX="8"
          refY="5"
          markerWidth="7"
          markerHeight="7"
          orient="auto-start-reverse"
        >
          <path d="M 0 0 L 10 5 L 0 10 z" class="factory-arrowhead" />
        </marker>
      </defs>
      {#each steps as step (step.id)}
        {#each step.after.filter((id) => layout.place[id]) as from (from)}
          <path
            class="factory-edge"
            d={forward(from, step.id)}
            marker-end="url(#factory-arrow)"
          />
        {/each}
      {/each}
      {#each layout.back as step, index (step.id)}
        {@const edge = backward(step, index)}
        <path
          class="factory-edge factory-edge--back"
          d={edge.path}
          marker-end="url(#factory-arrow)"
        />
        <text class="factory-edge-label" x={edge.label.x} y={edge.label.y}
          >{t('factory.diagram.returns', { rounds: step.rounds })}</text
        >
      {/each}
    </svg>
    <ol
      class="factory-diagram-steps"
      aria-label={t('factory.diagram.label', { name: workflow?.name ?? '' })}
    >
      {#each steps as step, index (step.id)}
        {@const at = layout.place[step.id]}
        {@const state = status?.(step) ?? ''}
        <li
          class="factory-node"
          data-step-id={step.id}
          data-step-status={state || undefined}
          data-selected={selectedId === step.id || undefined}
          aria-current={state === 'current' || state === 'waiting'
            ? 'step'
            : undefined}
          style:left={`${at.x}px`}
          style:top={`${at.y}px`}
          style:width={`${WIDTH}px`}
          style:height={`${HEIGHT}px`}
        >
          {#if step.approval}<span class="factory-node-gate"
              >{t('factory.diagram.approval')}</span
            >{/if}
          {#if onSelect}
            <button
              type="button"
              class="factory-node-body"
              aria-pressed={selectedId === step.id}
              onclick={() => onSelect(step.id)}
            >
              <strong>{index + 1}. {stepName(step, index)}</strong>
              <span>{agentLabel(step) || t('factory.diagram.noAgent')}</span>
            </button>
          {:else}
            <div class="factory-node-body">
              <strong>{stepName(step, index)}</strong>
              <span
                >{state
                  ? statusLabel(state)
                  : agentLabel(step)}{#if state && agentLabel(step)}
                  · {agentLabel(step)}{/if}</span
              >
            </div>
          {/if}
        </li>
      {/each}
    </ol>
  </div>
</div>

<style>
  .factory-diagram {
    overflow-x: auto;
    border: 1px solid var(--border);
    border-radius: 9px;
    background: var(--surface-2);
  }
  .factory-diagram-canvas {
    position: relative;
    min-width: 100%;
  }
  svg {
    position: absolute;
    inset: 0;
    overflow: visible;
  }
  .factory-edge {
    fill: none;
    stroke: var(--text-lo);
    stroke-width: 1.5;
  }
  .factory-edge--back {
    stroke: var(--amber);
    stroke-dasharray: 5 4;
  }
  .factory-arrowhead {
    fill: var(--text-lo);
    stroke: none;
  }
  .factory-edge-label {
    fill: var(--text-med);
    stroke: none;
    font-size: 11px;
    text-anchor: middle;
  }
  .factory-diagram-steps {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .factory-node {
    position: absolute;
  }
  .factory-node-body {
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 3px;
    width: 100%;
    height: 100%;
    box-sizing: border-box;
    padding: 8px 12px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface);
    color: var(--text-hi);
    font: inherit;
    font-size: 12px;
    text-align: left;
  }
  button.factory-node-body {
    cursor: pointer;
  }
  button.factory-node-body:hover {
    border-color: var(--text-lo);
  }
  .factory-node-body strong,
  .factory-node-body span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .factory-node-body span {
    color: var(--text-med);
    font-size: 11px;
  }
  [data-selected] .factory-node-body {
    border-color: var(--accent);
    box-shadow: 0 0 0 1px var(--accent);
  }
  [data-step-status='done'] .factory-node-body {
    background: var(--surface-3);
  }
  [data-step-status='current'] .factory-node-body,
  [data-step-status='waiting'] .factory-node-body {
    border-color: var(--accent);
    background: color-mix(in srgb, var(--accent) 12%, var(--surface));
  }
  [data-step-status='pending'] .factory-node-body strong {
    color: var(--text-med);
    font-weight: 500;
  }
  .factory-node-gate {
    position: absolute;
    top: -9px;
    left: 10px;
    z-index: 1;
    padding: 1px 6px;
    border: 1px solid var(--accent);
    border-radius: 9px;
    background: var(--surface);
    color: var(--accent);
    font-size: 10px;
    line-height: 14px;
  }
</style>
