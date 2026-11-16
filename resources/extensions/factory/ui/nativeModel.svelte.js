import { onMount, untrack } from 'svelte';
import { failureText } from './presentation.js';

export function createNativeFactoryModel(model) {
  let factories = $state([]),
    factory = $state(null),
    members = $state([]),
    tasks = $state([]),
    topics = $state([]),
    detail = $state(null),
    topic = $state(null);
  let loading = $state(true),
    error = $state(''),
    pending = $state('');
  let generation = 0,
    disposed = false,
    refreshPromise = null,
    again = false;
  let cursors = $state({});
  const pageCounts = {
    factory: 1,
    member: 1,
    task: 1,
    topic: 1,
    post: 1,
    events: 1,
    results: 1,
  };
  async function pages(resource, operation, args, key) {
    let result,
      after,
      rows = [];
    for (let page = 0; page < pageCounts[resource]; page++) {
      result = await model.call(operation, {
        ...args,
        ...(after ? { after } : {}),
      });
      rows.push(...(result[key] || []));
      after = result.next_after;
      if (!after) break;
    }
    return { ...result, [key]: rows };
  }
  async function taskPages(id) {
    const result = await model.call('task.get', { id });
    for (const resource of ['events', 'results']) {
      let after = result[`${resource}_after`];
      for (let page = 1; page < pageCounts[resource] && after; page++) {
        const next = await model.call('task.get', {
          id,
          [`${resource}_after`]: after,
        });
        result[resource] = [
          ...(result[resource] || []),
          ...(next[resource] || []),
        ];
        after = next[`${resource}_after`];
      }
      result[`${resource}_after`] = after;
    }
    return result;
  }
  async function more(resource) {
    pageCounts[resource]++;
    await refresh();
  }
  const parts = $derived(
    model.route.split('/').filter(Boolean).map(decodeURIComponent),
  );
  const view = $derived(
    parts[0] === 'task'
      ? 'task'
      : parts[0] === 'topic'
        ? 'topic'
        : parts[2] || 'board',
  );
  const factoryId = $derived(
    parts[0] === 'factory'
      ? parts[1]
      : detail?.task?.factory_id || topic?.topic?.factory_id || '',
  );
  async function refresh() {
    again = true;
    if (refreshPromise) return refreshPromise;
    refreshPromise = (async () => {
      while (again && !disposed) {
        again = false;
        const token = ++generation,
          route = model.route;
        try {
          const list = await pages(
            'factory',
            'factory.list',
            { limit: 100 },
            'factories',
          );
          let nextDetail = null,
            nextTopic = null;
          if (parts[0] === 'task') nextDetail = await taskPages(parts[1]);
          if (parts[0] === 'topic')
            nextTopic = await pages(
              'post',
              'topic.get',
              {
                id: parts[1],
                limit: 100,
              },
              'posts',
            );
          const id =
            parts[0] === 'factory'
              ? parts[1]
              : nextDetail?.task?.factory_id || nextTopic?.topic?.factory_id;
          const reads = id
            ? await Promise.all([
                model.call('factory.get', { id }),
                pages(
                  'member',
                  'member.list',
                  { factory_id: id, limit: 100 },
                  'members',
                ),
                pages(
                  'task',
                  'task.list',
                  { factory_id: id, limit: 100 },
                  'tasks',
                ),
                pages(
                  'topic',
                  'topic.list',
                  { factory_id: id, limit: 100 },
                  'topics',
                ),
              ])
            : null;
          if (disposed || route !== model.route || token !== generation) {
            again = !disposed;
            continue;
          }
          factories = list.factories || [];
          factory = reads?.[0].factory || null;
          members = reads?.[1].members || [];
          tasks = reads?.[2].tasks || [];
          topics = reads?.[3].topics || [];
          detail = nextDetail;
          topic = nextTopic;
          cursors = {
            factory: list.next_after,
            member: reads?.[1].next_after,
            task: reads?.[2].next_after,
            topic: reads?.[3].next_after,
            post: nextTopic?.next_after,
            events: nextDetail?.events_after,
            results: nextDetail?.results_after,
          };
          error = '';
        } catch (cause) {
          if (!disposed && route === model.route) error = failureText(cause);
        } finally {
          if (!disposed && route === model.route) loading = false;
        }
      }
    })().finally(() => {
      refreshPromise = null;
    });
    return refreshPromise;
  }
  async function mutate(operation, args) {
    if (pending) return null;
    pending = operation;
    error = '';
    try {
      const result = await model.call(operation, args);
      if (['member.create', 'factory.create'].includes(operation))
        await model.refresh({ full: true });
      await refresh();
      return result;
    } catch (cause) {
      error = failureText(cause);
      return null;
    } finally {
      pending = '';
    }
  }
  function accept(resource, record) {
    if (resource === 'factory') factory = record;
    if (resource === 'member')
      members = members.map((item) => (item.id === record.id ? record : item));
    if (resource === 'task') {
      tasks = tasks.map((item) => (item.id === record.id ? record : item));
      if (detail?.task.id === record.id) detail = { ...detail, task: record };
    }
  }
  let observedRoute = null;
  $effect(() => {
    const route = model.route;
    untrack(() => {
      if (route !== observedRoute) {
        loading = true;
        detail = null;
        topic = null;
        observedRoute = route;
        for (const key of Object.keys(pageCounts)) pageCounts[key] = 1;
      }
      void refresh();
    });
  });
  onMount(() => {
    const off = model.client.onInvalidation(() => void refresh());
    return () => {
      disposed = true;
      generation++;
      off();
    };
  });
  return {
    refresh,
    more,
    get cursors() {
      return cursors;
    },
    mutate,
    accept,
    get factories() {
      return factories;
    },
    get factory() {
      return factory;
    },
    get members() {
      return members;
    },
    get tasks() {
      return tasks;
    },
    get topics() {
      return topics;
    },
    get detail() {
      return detail;
    },
    get topic() {
      return topic;
    },
    get view() {
      return view;
    },
    get factoryId() {
      return factoryId;
    },
    get loading() {
      return loading;
    },
    get error() {
      return error;
    },
    get pending() {
      return pending;
    },
  };
}
