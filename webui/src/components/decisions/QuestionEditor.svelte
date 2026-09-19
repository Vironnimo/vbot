<script>
  import Button from '../ui/Button.svelte';
  import TextArea from '../ui/TextArea.svelte';
  import TextField from '../ui/TextField.svelte';
  import { t } from '$lib/i18n.js';
  let { question, onChange, onRemove, index } = $props();
  const componentId = $props.id();
  let keyError = $state('');
  const patch = (values) => onChange({ ...question, ...values });
  function changeChoice(index, key, value, event) {
    const entries = Object.entries(question.criteria);
    entries[index] = [key, value];
    if (new Set(entries.map(([name]) => name)).size !== entries.length) {
      keyError = t(
        'jev.duplicateId',
        'This ID is already in use. Choose a unique ID.',
      );
      if (event) event.target.value = Object.keys(question.criteria)[index];
      return;
    }
    keyError = '';
    patch({ criteria: Object.fromEntries(entries) });
  }
  function addChoice() {
    let suffix = Object.keys(question.criteria).length + 1;
    while (Object.hasOwn(question.criteria, `option_${suffix}`)) suffix++;
    patch({ criteria: { ...question.criteria, [`option_${suffix}`]: '' } });
  }
</script>

{#if keyError}<p class="jev-error" role="alert">{keyError}</p>{/if}

<article class="jev-question">
  <div class="jev-row">
    <span class="jev-number">{String(index + 1).padStart(2, '0')}</span>
    <strong
      >{question.type === 'noul'
        ? t('jev.noul', 'Yes / no · Noul')
        : question.type === 'choice'
          ? t('jev.choice', 'Choice')
          : t('jev.score', 'Score')}</strong
    >
    <Button variant="tertiary" onClick={onRemove}
      >{t('jev.remove', 'Remove')}</Button
    >
  </div>
  <div class="jev-field">
    <label for={`${componentId}-id`}>{t('jev.questionId', 'Answer ID')}</label>
    <TextField
      id={`${componentId}-id`}
      value={question.id}
      onInput={(value) => patch({ id: value })}
    />
  </div>
  <div class="jev-field">
    <label for={`${componentId}-instructions`}
      >{t('jev.question', 'Question')}</label
    >
    <TextArea
      id={`${componentId}-instructions`}
      value={question.instructions}
      rows={2}
      onInput={(value) => patch({ instructions: value })}
      placeholder={t(
        'jev.questionPlaceholder',
        'Ask one focused question about the supplied state…',
      )}
    />
  </div>
  {#if question.type === 'choice'}
    <p class="jev-help">
      {t(
        'jev.choiceHelp',
        'Define the possible answers. Include an “other” option when nothing may fit.',
      )}
    </p>
    {#each Object.entries(question.criteria) as [key, description], i (i)}
      <div class="jev-criterion">
        <TextField
          ariaLabel={t('jev.optionId', 'Option ID')}
          value={key}
          onInput={(value, event) => changeChoice(i, value, description, event)}
        />
        <TextField
          ariaLabel={t('jev.optionDescription', 'When this option applies')}
          value={description}
          onInput={(value) => changeChoice(i, key, value)}
        />
        <Button
          variant="tertiary"
          ariaLabel={t('jev.removeOption', 'Remove option')}
          onClick={() =>
            patch({
              criteria: Object.fromEntries(
                Object.entries(question.criteria).filter((_, n) => n !== i),
              ),
            })}>×</Button
        >
      </div>
    {/each}
    <Button variant="tertiary" onClick={addChoice}
      >{t('jev.addOption', 'Add option')}</Button
    >
  {:else if question.type === 'score'}
    <p class="jev-help">
      {t(
        'jev.scoreHelp',
        'Describe each level, from lowest to highest. Scores may fall between levels.',
      )}
    </p>
    {#each question.criteria as level, i (i)}
      <div class="jev-criterion jev-level">
        <span>{i}</span>
        <TextField
          ariaLabel={t('jev.level', 'Level {index}', { index: i })}
          value={level}
          onInput={(value) =>
            patch({
              criteria: question.criteria.map((old, n) =>
                n === i ? value : old,
              ),
            })}
        />
        <Button
          variant="tertiary"
          ariaLabel={t('jev.removeLevel', 'Remove level')}
          onClick={() =>
            patch({ criteria: question.criteria.filter((_, n) => n !== i) })}
          >×</Button
        >
      </div>
    {/each}
    <Button
      variant="tertiary"
      onClick={() => patch({ criteria: [...question.criteria, ''] })}
      >{t('jev.addLevel', 'Add level')}</Button
    >
  {:else}
    <p class="jev-help">
      {t(
        'jev.noulHelp',
        'Returns the probability of yes: 0 means no, 1 means yes, and 0.5 means uncertain.',
      )}
    </p>
    {#if question.criteria}
      {#each ['true', 'false'] as key (key)}
        <div class="jev-field">
          <span
            >{key === 'true'
              ? t('jev.whenYes', 'When yes applies')
              : t('jev.whenNo', 'When no applies')}</span
          >
          <TextField
            ariaLabel={key === 'true'
              ? t('jev.whenYes', 'When yes applies')
              : t('jev.whenNo', 'When no applies')}
            value={question.criteria[key]}
            onInput={(value) =>
              patch({ criteria: { ...question.criteria, [key]: value } })}
          />
        </div>
      {/each}
      <Button
        variant="tertiary"
        onClick={() => {
          const rest = { ...question };
          delete rest.criteria;
          onChange(rest);
        }}>{t('jev.removeCriteria', 'Remove clarifications')}</Button
      >
    {:else}
      <Button
        variant="tertiary"
        onClick={() => patch({ criteria: { true: '', false: '' } })}
        >{t('jev.clarify', 'Clarify yes and no')}</Button
      >
    {/if}
  {/if}
</article>
