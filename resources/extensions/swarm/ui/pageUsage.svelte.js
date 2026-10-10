// Background changes reload the Usage report at most this often; the last
// change of a burst is always followed by a reload.
const USAGE_REFRESH_INTERVAL_MS = 10_000;

// The selected Swarm's Usage report: one combined group report with its
// participant rows. `host` is the page model's side: `call`, the shown
// `selectedSwarm` and `activeTab`, `disposed` and the page `error`.
export function createSwarmPageUsage(host) {
  let report = $state(null);

  let participantUsage = $state([]);

  let usageRequest = 0;

  let usageFlight = null;

  // Counts background changes of the selected Swarm; an in-flight Usage
  // request is shared only while no change has arrived since it started.
  let usageChanges = 0;

  let usageStartedAt = -Infinity;

  let usageTimer = null;

  let loading = $state(false);

  const rows = $derived(
    participantUsage.flatMap(({ participant, report: participantReport }) => {
      const models = participantReport?.usage?.activity?.models ?? [];
      return (models.length ? models : [null]).map((model, index) => ({
        id: `${participant.id}:${index}`,
        participant: participant.display_name,
        model,
        modelName: model ? model.model : participant.model,
        participantRows: index === 0 ? Math.max(models.length, 1) : 0,
        toolCalls: participantReport?.usage?.activity?.tool_calls,
      }));
    }),
  );

  // Forgets the report of the Swarm shown before.
  function reset() {
    report = null;
    participantUsage = [];
  }

  // A background change of the selected Swarm, whichever tab shows.
  function noteChange() {
    usageChanges += 1;
  }

  // A background change reloads Usage at once when the last request is old
  // enough, otherwise when USAGE_REFRESH_INTERVAL_MS since it have passed.
  function schedule(swarm) {
    const wait = usageStartedAt + USAGE_REFRESH_INTERVAL_MS - Date.now();
    if (wait <= 0) return load(swarm);
    usageTimer ??= setTimeout(() => {
      usageTimer = null;
      if (host.disposed || host.activeTab !== 'usage' || !host.selectedSwarm)
        return;
      void load(host.selectedSwarm).catch((cause) => {
        if (!host.disposed) host.error = cause.message;
      });
    }, wait);
  }

  async function load(swarm = host.selectedSwarm) {
    if (!swarm) return;
    clearTimeout(usageTimer);
    usageTimer = null;
    const request = ++usageRequest;
    loading = true;
    if (
      usageFlight?.swarmId !== swarm.id ||
      usageFlight.changes !== usageChanges
    ) {
      const flight = {
        swarmId: swarm.id,
        changes: usageChanges,
        promise: host.call('swarms.usage', { swarm_id: swarm.id }),
      };
      usageFlight = flight;
      usageStartedAt = Date.now();
    }
    const flight = usageFlight;
    try {
      const result = await flight.promise;
      if (
        host.disposed ||
        request !== usageRequest ||
        host.selectedSwarm?.id !== swarm.id
      )
        return;
      report = result;
      participantUsage = (swarm.participants ?? []).map((participant) => ({
        participant,
        report: {
          usage: result.usage?.participants?.find(
            (row) => row.participant_id === participant.id,
          ),
        },
      }));
    } finally {
      if (usageFlight === flight) usageFlight = null;
      if (!host.disposed && request === usageRequest) loading = false;
    }
  }

  function destroy() {
    clearTimeout(usageTimer);
  }

  return {
    get report() {
      return report;
    },
    get loading() {
      return loading;
    },
    get rows() {
      return rows;
    },
    reset,
    noteChange,
    schedule,
    load,
    destroy,
  };
}
