(() => {
  'use strict';

  const panel = document.querySelector('[data-calculator-preview]');
  const calculator = globalThis.ScoreCalculator;
  if (!panel || !calculator) return;

  const config = JSON.parse(panel.querySelector('#match-calculator-config').textContent);
  panel.querySelectorAll('[data-calculator-section]').forEach(section => {
    section.innerHTML = calculator.sectionBodyHtml(config, section.dataset.calculatorSection);
  });

  const byData = (attribute) => new Map(
    [...panel.querySelectorAll(`[data-${attribute}]`)].map(element => [element.dataset[attribute], element])
  );
  const inputs = byData('path');
  const summary = { subs: byData('sub'), chips: byData('chip'), sums: byData('sum') };
  const details = panel.querySelector('[name="calculator_scores"]');
  const total = panel.querySelector('[name="calculator_total"]');
  const submit = panel.closest('form').querySelector('[data-calculator-submit]');
  const normalized = new Map([
    ['basic:original', calculator.amountText],
    ['basic:time', calculator.timeText],
  ]);
  calculator.eachItem(config, (item, path) => {
    if (item.type === 'number' || item.type === 'threshold') {
      normalized.set(path, value => calculator.quantityText(value, item));
    }
  });

  if (details) {
    const draft = JSON.parse(panel.querySelector('#match-calculator-draft').textContent);
    if (draft) {
      const restore = (path, value) => {
        const input = inputs.get(path);
        if (input?.type === 'checkbox') input.checked = value === true;
        else if (input && typeof value === 'string') input.value = calculator.normalizeNumeric(value);
      };
      restore('basic:original', draft.original);
      restore('basic:time', draft.time);
      calculator.eachItem(config, (item, path) => restore(path, draft.items?.[item.id]));
    }
  }

  function refresh() {
    const scores = calculator.readScores(config, path => {
      const input = inputs.get(path);
      return input?.type === 'checkbox' ? input.checked : input?.value;
    });
    const result = calculator.calculate(config, scores);
    calculator.paintPreview(config, result, summary);
    if (details) {
      details.value = JSON.stringify(scores);
      total.value = String(result.total);
      submit.disabled = false;
    }
  }

  function inputChanged(event) {
    const input = event.target;
    if (event.isComposing || !input.matches('[data-path]')) return;
    if (input.hasAttribute('data-num')) {
      const value = calculator.normalizeNumeric(input.value);
      if (input.value !== value) input.value = value;
    }
    refresh();
  }

  panel.addEventListener('input', inputChanged);
  panel.addEventListener('compositionend', inputChanged);
  panel.addEventListener('change', event => {
    const input = event.target;
    const normalize = normalized.get(input.dataset.path);
    if (normalize) {
      input.value = normalize(input.value);
      refresh();
    }
  });
  panel.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.isComposing && event.target.matches('[data-path]')) {
      event.preventDefault();
    }
  });

  refresh();
  panel.querySelector('[data-calculator-content]').hidden = false;
  panel.querySelector('[data-calculator-unavailable]').hidden = true;
})();
