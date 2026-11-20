import { t } from '@vbot/extension-sdk/lib/i18n.js';

const domain = {
  key: 'domain',
  label: () => t('haWakeword.domain'),
  example: 'light',
};
const entity = {
  key: 'entity_id',
  label: () => t('haWakeword.entity'),
  example: 'light.kitchen',
};
export const toolFields = {
  ha_call_service: [
    domain,
    {
      key: 'service',
      label: () => t('haWakeword.service'),
      example: 'turn_on',
    },
    entity,
  ],
  ha_get_state: [entity],
  ha_list_entities: [
    domain,
    { key: 'area', label: () => t('haWakeword.area'), example: 'kitchen' },
  ],
  ha_list_services: [domain],
};

export function commandDraft(command) {
  const tool = command?.tool ?? 'ha_call_service';
  const extra = { ...command?.arguments };
  const values = {};
  for (const { key } of toolFields[tool]) {
    if (typeof extra[key] === 'string') {
      values[key] = extra[key];
      delete extra[key];
    }
  }
  return {
    phrase: command?.phrase ?? '',
    aliases: (command?.aliases ?? []).join('\n'),
    tool,
    values,
    extra: Object.keys(extra).length ? JSON.stringify(extra, null, 2) : '',
  };
}

export function draftCommand(draft, previous) {
  const phrase = draft.phrase.trim();
  if (!phrase) throw new Error(t('haWakeword.phraseRequired'));
  let args;
  try {
    args = draft.extra.trim() ? JSON.parse(draft.extra) : {};
    if (!args || typeof args !== 'object' || Array.isArray(args))
      throw new Error();
  } catch {
    throw new Error(t('haWakeword.invalidJson'));
  }
  for (const { key } of toolFields[draft.tool]) {
    const value = draft.values[key]?.trim();
    if (!value) continue;
    if (Object.hasOwn(args, key))
      throw new Error(t('haWakeword.duplicateArgument', { key }));
    args[key] = value;
  }
  return {
    ...previous,
    phrase,
    aliases: draft.aliases
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean),
    tool: draft.tool,
    arguments: args,
  };
}
