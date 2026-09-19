import { t } from '$lib/i18n.js';

export function newQuestion(type, id) {
  return {
    id,
    type,
    instructions: '',
    ...(type === 'choice' ? { criteria: { option_a: '', option_b: '' } } : {}),
    ...(type === 'score' ? { criteria: ['', '', ''] } : {}),
  };
}

export function experimentExample(kind = 'blank') {
  if (kind === 'triage')
    return {
      title: t('jev.example.triage', 'Support triage'),
      state:
        'Since the update, the export button does nothing. I tried two browsers. Everything else still works, but I need the export for a report tomorrow.',
      questions: [
        {
          id: 'category',
          type: 'choice',
          instructions: 'What is the primary purpose of this message?',
          criteria: {
            bug: 'Reporting broken functionality',
            feature: 'Requesting new functionality',
            question: 'Asking how something works',
            other: 'None of these',
          },
        },
        {
          id: 'urgency',
          type: 'score',
          instructions: 'How urgently does the user need help?',
          criteria: [
            'No deadline or blockage',
            'A deadline soon, with part of the product still usable',
            'Immediate deadline or complete blockage',
          ],
        },
        {
          id: 'workaround',
          type: 'noul',
          instructions:
            'Does the message explicitly say a working workaround is available?',
        },
      ],
    };
  if (kind === 'routing')
    return {
      title: t('jev.example.routing', 'Task requirements'),
      state:
        'Review a change to our payment retry logic. Check whether simultaneous retries could charge a customer twice and propose a fix with tests.',
      questions: [
        {
          id: 'complexity',
          type: 'score',
          instructions:
            'What level of reasoning does this task appear to need?',
          criteria: [
            'Straightforward transformation with explicit steps',
            'Several related steps requiring interpretation',
            'Interacting conditions or failure modes requiring careful analysis',
          ],
        },
        {
          id: 'code',
          type: 'noul',
          instructions:
            'Does completing this task require reading or modifying source code?',
        },
        {
          id: 'consequences',
          type: 'noul',
          instructions:
            'Could an incorrect result plausibly cause financial loss or data loss?',
        },
      ],
    };
  return {
    title: t('jev.newTitle', 'Untitled experiment'),
    state: '',
    questions: [newQuestion('noul', 'question_1')],
  };
}
