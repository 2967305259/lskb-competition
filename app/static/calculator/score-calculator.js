(() => {
  'use strict';

  const sectionKeys = Object.freeze(['basic', 'levelBonus', 'extraBonus', 'penalty']);
  const zero = { coefficient: 0n, scale: 0 };
  const decimalPattern = /^([+-]?)(\d*)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/;
  const amountPattern = /^-?(?:\d+(?:\.\d*)?|\.\d+)$/;

  function decimal(value) {
    const match = decimalPattern.exec(String(value ?? ''));
    if (!match || !(match[2] || match[3]) || !Number.isFinite(Number(value))) return zero;
    const exponent = Number(match[4] || 0);
    if (Math.abs(exponent) > 324) return zero;
    const fraction = match[3] || '';
    const scale = fraction.length - exponent;
    const coefficient = BigInt(match[1] + (match[2] || '0') + fraction);
    return scale < 0
      ? { coefficient: coefficient * 10n ** BigInt(-scale), scale: 0 }
      : { coefficient, scale };
  }

  function align(value, scale) {
    return value.coefficient * 10n ** BigInt(scale - value.scale);
  }

  function add(left, right) {
    const scale = Math.max(left.scale, right.scale);
    return { coefficient: align(left, scale) + align(right, scale), scale };
  }

  function subtract(left, right) {
    return add(left, { coefficient: -right.coefficient, scale: right.scale });
  }

  function multiply(left, right) {
    return { coefficient: left.coefficient * right.coefficient, scale: left.scale + right.scale };
  }

  function max(left, right) {
    return subtract(left, right).coefficient >= 0n ? left : right;
  }

  function roundInteger(value, places) {
    if (value.scale <= places) return align(value, places);
    const divisor = 10n ** BigInt(value.scale - places);
    const whole = value.coefficient / divisor;
    const twiceRemainder = (value.coefficient % divisor) * 2n;
    if (twiceRemainder >= divisor) return whole + 1n;
    if (twiceRemainder < -divisor) return whole - 1n;
    return whole;
  }

  function rounded(value) {
    return Number(roundInteger(value, 2)) / 100;
  }

  function decimalText(value) {
    const { coefficient, scale } = value;
    if (!scale) return String(coefficient);
    const sign = coefficient < 0n ? '-' : '';
    const digits = String(coefficient < 0n ? -coefficient : coefficient).padStart(scale + 1, '0');
    return sign + digits.slice(0, -scale) + '.' + digits.slice(-scale);
  }

  function normalizeNumeric(raw) {
    let value = String(raw ?? '');
    value = value.replace(/[\uFF10-\uFF19]/g, digit => String.fromCharCode(digit.charCodeAt(0) - 0xFEE0));
    value = value.replace(/[\u2212\uFF0D\u2013\u2014\uFE63]/g, '-');
    value = value.replace(/[,.\uFF0C\u3001\u3002\uFF0E\uFF61\u00B7\u30FB\u2027\u2219\u066B]/g, '.');
    value = value.replace(/[^0-9.\-]/g, '');
    const sign = value.startsWith('-') ? '-' : '';
    value = value.replace(/-/g, '');
    const dot = value.indexOf('.');
    if (dot >= 0) value = value.slice(0, dot + 1) + value.slice(dot + 1).replace(/\./g, '');
    return sign + value;
  }

  function amountText(raw) {
    const value = normalizeNumeric(raw);
    return amountPattern.test(value) && Number.isFinite(Number(value)) ? value : '';
  }

  function quantityText(raw, item) {
    const value = amountText(raw);
    const min = decimal(item.min);
    return value && subtract(decimal(value), min).coefficient < 0n ? decimalText(min) : value;
  }

  function timeText(raw) {
    const value = amountText(raw);
    return decimal(value).coefficient < 0n ? '0' : value;
  }

  function eachItem(config, visit) {
    for (const key of sectionKeys) {
      (config[key].groups || []).forEach((group, groupIndex) => {
        (group.items || []).forEach((item, itemIndex) => {
          visit(item, `${key}:${groupIndex}:${itemIndex}`, key, group);
        });
      });
    }
  }

  function readScores(config, read) {
    const items = Object.create(null);
    eachItem(config, (item, path) => {
      if (!item.id || item.type === 'fixed') return;
      const value = read(path);
      items[item.id] = item.type === 'check' ? value === true : quantityText(value, item);
    });
    return { original: amountText(read('basic:original')), time: timeText(read('basic:time')), items };
  }

  function timeScore(basic, raw) {
    const rules = basic.timeRules || [];
    if (raw === '' || !rules.length) return zero;
    const time = decimal(raw);
    let lastBound = decimal(rules[0].max);
    for (const rule of rules) {
      const upper = decimal(rule.max);
      if (subtract(time, decimal(rule.min)).coefficient >= 0n && subtract(time, upper).coefficient <= 0n) {
        return decimal(rule.score);
      }
      lastBound = max(lastBound, upper);
    }
    const over = subtract(time, lastBound);
    if (over.coefficient <= 0n) return zero;
    if (basic.timeOverflow.rounding === 'exact') return multiply(over, decimal(basic.timeOverflow.perHour));
    const divisor = 10n ** BigInt(over.scale);
    const units = { coefficient: (over.coefficient + divisor - 1n) / divisor, scale: 0 };
    return multiply(units, decimal(basic.timeOverflow.perHour));
  }

  function itemScore(item, input) {
    const score = decimal(item.score);
    if (item.type === 'check') return input === true ? score : zero;
    if (item.type === 'fixed') return score;
    const quantity = max(decimal(item.min), decimal(amountText(input)));
    const units = item.type === 'threshold' ? max(zero, subtract(quantity, decimal(item.threshold))) : quantity;
    return multiply(units, score);
  }

  function calculate(config, scores, readItem = (path, item) => {
    const items = scores.items || {};
    return Object.prototype.hasOwnProperty.call(items, item.id) ? items[item.id] : undefined;
  }) {
    const original = decimal(amountText(scores.original));
    const time = timeScore(config.basic, timeText(scores.time));
    const sectionTotals = Object.fromEntries(sectionKeys.map(key => [key, zero]));
    const itemScores = {};
    eachItem(config, (item, path, key) => {
      const subtotal = itemScore(item, readItem(path, item));
      sectionTotals[key] = add(sectionTotals[key], subtotal);
      itemScores[path] = rounded(subtotal);
    });

    const basicSub = add(time, sectionTotals.basic);
    const bonus = add(add(basicSub, sectionTotals.levelBonus), sectionTotals.extraBonus);
    const penalty = sectionTotals.penalty;
    const totalCents = roundInteger(add(add(original, bonus), penalty), 2);
    return {
      original: rounded(original),
      time: rounded(time),
      sections: Object.fromEntries(sectionKeys.map(key => [key, rounded(sectionTotals[key])])),
      basicSub: rounded(basicSub),
      bonus: rounded(bonus),
      penalty: rounded(penalty),
      total: Number(totalCents) / 100,
      // 成绩列用已经舍入到分的总分再取整，避免二进制浮点在半分边界改变结果。
      matchScore: Number(roundInteger({ coefficient: totalCents, scale: 2 }, 0)),
      itemScores
    };
  }

  const escape = value => String(value ?? '').replace(/[&<>"']/g,
    char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
  const roundScore = value => rounded(decimal(value));
  const signed = value => {
    const score = roundScore(value);
    return (score > 0 ? '+' : '') + score;
  };
  const ruleNumberText = value => decimalText(decimal(value));
  const signedRule = value => {
    const number = decimal(value);
    return (number.coefficient > 0n ? '+' : '') + decimalText(number);
  };

  function amountInput(path, label, nonNegative) {
    return `<input type="text" class="num" inputmode="decimal" autocomplete="off" placeholder="0"
      aria-label="${escape(label)}" data-num${nonNegative ? ' data-nonneg' : ''} data-path="${path}">`;
  }

  function itemHtml(path, item) {
    const name = escape(item.name);
    const value = signed(item.score);
    if (item.type === 'check') {
      return `<label class="item item--check">
        <input type="checkbox" data-path="${path}">
        <span class="item-name">${name}</span>
        <span class="item-score" data-sub="${path}">${value}</span>
      </label>`;
    }
    if (item.type === 'fixed') {
      return `<div class="item">
        <span class="item-name">${name}<span class="hint">固定计入</span></span>
        <span class="item-score" data-sub="${path}">${value}</span>
      </div>`;
    }
    const unit = escape(item.unit || '个');
    const rate = signedRule(item.score);
    const hint = item.type === 'threshold'
      ? `超出 ${ruleNumberText(item.threshold)}${unit}后，每${unit} ${rate}`
      : `每${unit} ${rate}`;
    return `<div class="item item--input">
      <span class="item-name">${name}<span class="hint">${hint}</span></span>
      ${amountInput(path, item.name, decimal(item.min).coefficient >= 0n)}
      <span class="unit">${unit}</span>
      <span class="item-sub" data-sub="${path}">0</span>
    </div>`;
  }

  function timeHint(basic) {
    const rules = basic.timeRules || [];
    if (!rules.length) return '未设置时段规则，用时不计分';
    const parts = rules.map(rule => `${ruleNumberText(rule.min)}~${ruleNumberText(rule.max)}h ${signedRule(rule.score)}`);
    const lastBound = rules.map(rule => decimal(rule.max)).reduce(max);
    const tail = basic.timeOverflow.rounding === 'exact' ? '（按实际小数计）' : '（不足 1h 按 1h 计）';
    parts.push(`超 ${decimalText(lastBound)}h 每超 1h ${signedRule(basic.timeOverflow.perHour)}${tail}`);
    return parts.join('；');
  }

  function sectionBodyHtml(config, key) {
    const section = config[key];
    const groups = (section.groups || []).map((group, groupIndex) => `<div class="group">
      ${group.title ? `<div class="group-title">${escape(group.title)}</div>` : ''}
      <div class="items">${(group.items || []).map((item, itemIndex) =>
        itemHtml(`${key}:${groupIndex}:${itemIndex}`, item)).join('')}</div>
    </div>`).join('');
    if (key !== 'basic') return groups;
    return `<div class="fields">
      <div class="field">
        <span class="field-label"><span class="name">${escape(section.scoreLabel)}</span><span class="hint">${escape(section.scoreHint)}</span></span>
        <span class="field-input">${amountInput('basic:original', section.scoreLabel, false)}</span>
      </div>
      <div class="field">
        <span class="field-label"><span class="name">${escape(section.timeLabel)}</span><span class="hint">${escape(timeHint(section))}</span></span>
        <span class="field-input">${amountInput('basic:time', section.timeLabel, true)}<span class="item-sub" data-sub="basic:time">0</span></span>
      </div>
    </div>${groups}`;
  }

  function paintSub(subs, path, value, text) {
    const el = subs.get(path);
    if (!el) return;
    const v = roundScore(value);
    el.textContent = text === undefined ? signed(v) : text;
    el.classList.toggle('is-on-pos', v > 0);
    el.classList.toggle('is-on-neg', v < 0);
  }

  function paintChip(chips, key, value) {
    const el = chips.get(key);
    if (!el) return;
    const v = roundScore(value);
    el.textContent = '本区小计 ' + signed(v);
    el.classList.toggle('pos', v > 0);
    el.classList.toggle('neg', v < 0);
  }

  function paintSum(sums, key, text, tone) {
    const el = sums.get(key);
    if (!el) return;
    el.textContent = text;
    el.classList.toggle('pos', tone === 'pos');
    el.classList.toggle('neg', tone === 'neg');
  }

  function paintPreview(config, t, { subs, chips, sums }) {
    paintSub(subs, 'basic:time', t.time);
    eachItem(config, (item, path) => {
      if (item.type === 'check') paintSub(subs, path, t.itemScores[path], signed(item.score));
      else if (item.type !== 'fixed') paintSub(subs, path, t.itemScores[path]);
    });

    paintChip(chips, 'basic', t.basicSub);
    paintChip(chips, 'levelBonus', t.sections.levelBonus);
    paintChip(chips, 'extraBonus', t.sections.extraBonus);
    paintChip(chips, 'penalty', t.sections.penalty);

    paintSum(sums, 'original', signed(t.original), '');
    paintSum(sums, 'bonus', signed(t.bonus), t.bonus >= 0 ? 'pos' : 'neg');
    paintSum(sums, 'penalty', signed(t.penalty), t.penalty < 0 ? 'neg' : '');
    paintSum(sums, 'total', signed(t.total), t.total > 0 ? 'pos' : (t.total < 0 ? 'neg' : ''));
  }

  globalThis.ScoreCalculator = Object.freeze({
    sectionKeys, normalizeNumeric, amountText, quantityText, timeText, eachItem, readScores,
    calculate, paintPreview, ruleNumberText, roundScore, signed, sectionBodyHtml
  });
})();
