// Core owns these format rules. Browser checks protect local storage; only the
// server creates Secrets and replaces submitted text with usable references.
import specification from '../../../core/secrets/api_key_rules.json';

const token = specification.token_characters;
const rules = specification.rules.map((rule) => ({
  ...rule,
  expression: new RegExp(
    `(?<![${token}])(?:${rule.pattern})(?![${token}])`,
    'g',
  ),
}));
const prefixExpression = new RegExp(
  `(?<![${token}])(?:${specification.draft_prefixes.join('|')})`,
);

function entropy(value) {
  const counts = new Map();
  for (const character of value) {
    counts.set(character, (counts.get(character) ?? 0) + 1);
  }
  let result = 0;
  for (const count of counts.values()) {
    const probability = count / value.length;
    result -= probability * Math.log2(probability);
  }
  return result;
}

export function detectApiKeys(text) {
  if (typeof text !== 'string') return [];
  const found = [];
  for (const rule of rules) {
    for (const match of text.matchAll(rule.expression)) {
      if (entropy(match[1]) < specification.minimum_entropy) continue;
      found.push({
        start: match.index,
        end: match.index + match[0].length,
        value: match[0],
        name: rule.name,
        provider: rule.provider,
      });
    }
  }
  return found.sort((left, right) => left.start - right.start);
}

export function containsApiKey(text) {
  return detectApiKeys(text).length > 0;
}

export function containsPotentialApiKey(text) {
  return typeof text === 'string' && prefixExpression.test(text);
}
