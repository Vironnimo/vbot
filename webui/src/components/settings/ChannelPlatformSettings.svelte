<script>
  import Dropdown from '../Dropdown.svelte';
  import TextField from '../ui/TextField.svelte';
  import TextArea from '../ui/TextArea.svelte';
  import { t } from '$lib/i18n.js';
  import Toggle from '../ui/Toggle.svelte';
  import ChannelPlatformSettings from './ChannelPlatformSettings.svelte';

  let { schema = {}, value = {}, disabled = false, onChange } = $props();
  const uid = $props.id();
  const properties = $derived(Object.entries(schema.properties ?? {}));

  function jsonValue(text) {
    try {
      return JSON.parse(text);
    } catch {
      return text;
    }
  }

  function jsonText(current, fallback) {
    return typeof current === 'string'
      ? current
      : JSON.stringify(current ?? fallback, null, 2);
  }

  function setField(key, next) {
    const updated = { ...value };
    if (next === undefined) delete updated[key];
    else updated[key] = next;
    onChange(updated);
  }

  function scalarValue(field, text) {
    if (field.type === 'number' || field.type === 'integer') {
      if (!text.trim()) return undefined;
      const number = Number(text.replace(',', '.'));
      return Number.isFinite(number) ? number : text;
    }
    return text;
  }
</script>

{#each properties as [key, field] (key)}
  {@const label = field.title || key}
  {#if field.type === 'object'}
    <fieldset {disabled}>
      <legend>{label}</legend>
      <ChannelPlatformSettings
        schema={field}
        value={value[key] ?? {}}
        {disabled}
        onChange={(next) => setField(key, next)}
      />
    </fieldset>
  {:else}
    <div class="s-row">
      <div class="s-row-info">
        <label class="s-row-label" for={`${uid}-${key}`}>{label}</label>
        {#if field.description}
          <div class="s-row-desc">{field.description}</div>
        {/if}
      </div>
      <div class="s-row-control">
        {#if Array.isArray(field.enum)}
          <Dropdown
            id={`${uid}-${key}`}
            value={JSON.stringify(value[key]) ?? ''}
            options={field.enum.map((item) => ({
              value: JSON.stringify(item),
              label: String(item),
            }))}
            ariaLabel={label}
            {disabled}
            onValueChange={(next) => setField(key, JSON.parse(next))}
          />
        {:else if field.type === 'array' || !['string', 'number', 'integer', 'boolean'].includes(field.type)}
          <TextArea
            id={`${uid}-${key}`}
            value={jsonText(value[key], field.type === 'array' ? [] : null)}
            ariaLabel={label}
            code
            rows={3}
            {disabled}
            onInput={(next) => setField(key, jsonValue(next))}
          />
        {:else if field.type === 'boolean'}
          <Toggle
            id={`${uid}-${key}`}
            checked={value[key] === true}
            ariaLabel={label}
            {disabled}
            onChange={(next) => setField(key, next)}
          />
        {:else}
          <TextField
            id={`${uid}-${key}`}
            value={Array.isArray(value[key])
              ? value[key].join(', ')
              : String(value[key] ?? '')}
            ariaLabel={label}
            required={schema.required?.includes(key) ?? false}
            {disabled}
            onInput={(next) => setField(key, scalarValue(field, next))}
          />
        {/if}
      </div>
    </div>
  {/if}
{/each}

{#if schema.type === 'object' && (properties.length > 0 || schema.additionalProperties !== false)}
  <details>
    <summary>{t('settings.title')} (JSON)</summary>
    <TextArea
      value={jsonText(value, {})}
      ariaLabel={`${t('settings.title')} (JSON)`}
      code
      rows={5}
      {disabled}
      onInput={(next) => onChange(jsonValue(next))}
    />
  </details>
{/if}
