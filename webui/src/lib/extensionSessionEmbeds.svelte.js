import { SvelteMap } from 'svelte/reactivity';
import {
  cancelExtensionPageToolCall,
  resolveExtensionPageSession,
} from './api.js';
import { t } from './i18n.js';
import { sessionPresentations } from './extensionChatContext.js';

function validBounds(value) {
  const coordinate = (n) => Number.isSafeInteger(n) && Math.abs(n) <= 1_000_000;
  return (
    value &&
    [value.x, value.y, value.width, value.height].every(coordinate) &&
    value.width > 0 &&
    value.height > 0 &&
    Array.isArray(value.clip) &&
    value.clip.length === 4 &&
    value.clip.every((n) => coordinate(n) && n >= 0)
  );
}

export function createExtensionSessionEmbeds() {
  const slots = new SvelteMap();

  async function mount(context, params) {
    const { slot, group_id, participant_id } = params;
    const presentation = params.presentation ?? 'conversation';
    if (
      ![slot, group_id, participant_id].every(
        (id) => typeof id === 'string' && id.length > 0 && id.length <= 256,
      ) ||
      !sessionPresentations.includes(presentation) ||
      slots.has(slot) ||
      slots.size >= 16
    )
      throw new Error(t('extensions.page.requestInvalid'));
    const registration = {
      id: context.descriptor.page,
      epoch: context.descriptor.epoch,
    };
    const { owner } = context.descriptor;
    const pending = Symbol();
    slots.set(slot, { pending, address: null, bounds: null });
    try {
      const address = await resolveExtensionPageSession(
        owner,
        registration,
        group_id,
        participant_id,
      );
      const current = slots.get(slot);
      if (current?.pending !== pending) return;
      const monitor = {
        cancelToolCall: ({ runId, toolCallId }) =>
          cancelExtensionPageToolCall(
            owner,
            registration,
            group_id,
            runId,
            toolCallId,
          ),
      };
      slots.set(slot, {
        address,
        bounds: current.bounds,
        visible: current.visible,
        monitor,
        presentation,
      });
    } catch (error) {
      if (slots.get(slot)?.pending === pending) slots.delete(slot);
      throw error;
    }
  }

  function position(slot, bounds) {
    const current = slots.get(slot);
    if (current) {
      const visible = validBounds(bounds);
      slots.set(slot, {
        ...current,
        visible,
        bounds: visible ? bounds : current.bounds,
      });
    }
  }

  return { slots, mount, position };
}
