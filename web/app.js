/* 阿里云余额批量看板 —— 前端逻辑
   无外部依赖；所有密钥操作都在本地服务端完成，这里只碰掩码与汇总。 */

'use strict';

(() => {
  const DEFAULT_ENDPOINT = 'business.aliyuncs.com';
  const LS = { theme: 'abb.theme', settings: 'abb.settings', pos: 'abb.widget.pos' };
  const AVATAR_COLORS = [
    ['#ffb26b', '#f05a00'],
    ['#8ec5ff', '#2f6fed'],
    ['#8ee0b3', '#0d8a4f'],
    ['#c9b8ff', '#7048d8'],
    ['#ffb3c0', '#d92d5c'],
    ['#f8d477', '#c47b00'],
  ];
  const ERROR_HINTS = {
    'InvalidAccessKeyId.NotFound': 'AccessKey 不存在，请检查 AK 是否复制完整',
    'InvalidAccessKeyId.Inactive': 'AccessKey 已禁用',
    'SignatureDoesNotMatch': 'Secret 不正确或已轮换，请核对 SK',
    'Forbidden.RAM': '该 AK 缺少 BSS 只读权限，请授予 AliyunBSSReadOnlyAccess',
    'NoPermission': '该 AK 缺少 BSS 只读权限',
    'Forbidden': '没有权限调用该接口',
    'Throttling': '请求过于频繁，请稍后再试',
    'Throttling.User': '请求过于频繁，请稍后再试',
    'InvalidTimeStamp': '本机时间偏差过大，请校准系统时间',
    'NetworkError': '网络不可达，请检查代理 / 防火墙',
    'ServiceUnavailable': '阿里云服务暂时不可用，请稍后再试',
    'BadResponse': '阿里云返回了无法解析的响应',
  };
  const COUPON_STATUS = {
    available: { label: '可用', cls: 'ok' },
    availabletouse: { label: '可用', cls: 'ok' },
    used: { label: '已用', cls: 'muted' },
    expired: { label: '已过期', cls: 'bad' },
    cancelled: { label: '已作废', cls: 'muted' },
  };
  const SHORT_HINTS = {
    'InvalidAccessKeyId.NotFound': 'AccessKey 不存在',
    'InvalidAccessKeyId.Inactive': 'AccessKey 已禁用',
    'SignatureDoesNotMatch': 'SK 不正确',
    'Forbidden.RAM': '缺少 BSS 只读权限',
    'NoPermission': '缺少 BSS 只读权限',
    'Throttling': '请求过于频繁',
    'NetworkError': '网络不可达',
    'ServiceUnavailable': '服务暂不可用',
  };

  const state = {
    accounts: [],
    results: new Map(),
    loading: new Set(),
    querying: false,
    lastAt: 0,
    filter: '',
    protection: 'dpapi',
    demo: new URLSearchParams(location.search).get('demo') === '1',
    theme: 'auto',
    settings: { autoMin: 5, widget: true },
    editingId: null,
    accountTab: 'single',
  };
  let autoTimer = null;
  let clickTimer = null;
  let suppressWidgetUntil = 0;
  let confirmHandler = null;
  let batchPreviewTimer = null;

  // ---------------------------------------------------------------- 基础工具
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const icon = (name, cls = 'icon') => `<svg class="${cls}"><use href="#i-${name}"></use></svg>`;
  const clamp = (value, min, max) => Math.min(Math.max(value, min), max);
  const pad2 = (n) => String(n).padStart(2, '0');

  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[ch]));
  }

  function num(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n : 0;
  }

  const moneyFormat = new Intl.NumberFormat('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });

  function fmtMoney(value) {
    const n = Number(value);
    return Number.isFinite(n) ? moneyFormat.format(n) : '—';
  }

  function symbol(code) {
    return ({ CNY: '¥', USD: '$', HKD: 'HK$', JPY: 'JP¥', EUR: '€' }[code] || (code ? `${code} ` : '¥'));
  }

  function amount(value, code = 'CNY') {
    if (value === null || value === undefined || value === '') return '—';
    const n = Number(value);
    if (!Number.isFinite(n)) return '—';
    return `${symbol(code)}${fmtMoney(n)}`;
  }

  function fmtTime(ts) {
    if (!ts) return '—';
    const d = new Date(ts);
    return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
  }

  function fmtShort(ts) {
    if (!ts) return '—';
    const d = new Date(ts);
    return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
  }

  function fmtDate(ts) {
    if (!ts) return '—';
    const d = new Date(ts);
    return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
  }

  function daysUntil(ts) {
    if (!ts) return null;
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const target = new Date(ts);
    target.setHours(0, 0, 0, 0);
    return Math.round((target - today) / 86400000);
  }

  function relativeExpiry(ts) {
    const days = daysUntil(ts);
    if (days === null) return '';
    if (days < 0) return '已过期';
    if (days === 0) return '今天到期';
    if (days === 1) return '明天到期';
    return `${days} 天后到期`;
  }

  function expiryClass(ts) {
    const days = daysUntil(ts);
    if (days === null) return 'muted';
    if (days <= 7) return 'bad';
    if (days <= 30) return 'warn';
    return '';
  }

  function initial(name) {
    const text = String(name || '云').trim();
    return text ? text.slice(0, 1) : '云';
  }

  function avatarStyle(name) {
    let hash = 0;
    for (const ch of String(name || '')) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
    const [a, b] = AVATAR_COLORS[hash % AVATAR_COLORS.length];
    return `--av1:${a};--av2:${b}`;
  }

  function friendlyError(error) {
    if (!error) return '查询失败';
    return ERROR_HINTS[error.code] || error.message || error.code || '查询失败';
  }

  function shortError(error) {
    if (!error) return '';
    return SHORT_HINTS[error.code] || (error.message || error.code || '查询失败').slice(0, 16);
  }

  function toast(message, type = 'ok') {
    const stack = $('#toastStack');
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.innerHTML = `${icon(type === 'ok' ? 'check' : 'alert')}<span>${esc(message)}</span>`;
    stack.appendChild(el);
    setTimeout(() => {
      el.style.opacity = '0';
      el.style.transform = 'translateY(4px)';
      setTimeout(() => el.remove(), 220);
    }, 3400);
  }

  function storeGet(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      return raw === null ? fallback : JSON.parse(raw);
    } catch {
      return fallback;
    }
  }

  function storeSet(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch { /* 隐私模式下忽略 */ }
  }

  // ---------------------------------------------------------------- 设置 / 主题
  function loadPrefs() {
    const theme = storeGet(LS.theme, null);
    if (theme === 'light' || theme === 'dark' || theme === 'auto') state.theme = theme;
    const settings = storeGet(LS.settings, null);
    if (settings && typeof settings === 'object') {
      if ([0, 1, 5, 15, 30].includes(Number(settings.autoMin))) state.settings.autoMin = Number(settings.autoMin);
      if (typeof settings.widget === 'boolean') state.settings.widget = settings.widget;
    }
    const urlTheme = new URLSearchParams(location.search).get('theme');
    if (urlTheme === 'light' || urlTheme === 'dark') state.theme = urlTheme;
  }

  function savePrefs() {
    storeSet(LS.theme, state.theme);
    storeSet(LS.settings, state.settings);
  }

  function applyTheme() {
    const dark = state.theme === 'dark'
      || (state.theme === 'auto' && window.matchMedia('(prefers-color-scheme: dark)').matches);
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    const iconName = state.theme === 'auto' ? 'auto' : (dark ? 'moon' : 'sun');
    const use = $('#themeIcon use');
    if (use) use.setAttribute('href', `#i-${iconName}`);
    const label = state.theme === 'auto' ? '跟随系统' : (state.theme === 'dark' ? '深色' : '浅色');
    $('#btnTheme').title = `主题：${label}（点击切换）`;
    $('#btnTheme').setAttribute('aria-label', `主题：${label}，点击切换`);
    const select = $('#setTheme');
    if (select) select.value = state.theme;
  }

  function cycleTheme() {
    state.theme = state.theme === 'auto' ? 'light' : (state.theme === 'light' ? 'dark' : 'auto');
    savePrefs();
    applyTheme();
  }

  // ---------------------------------------------------------------- API
  async function api(path, options = {}) {
    const { method = 'GET', body } = options;
    let response;
    try {
      response = await fetch(path, {
        method,
        headers: { 'X-Balance-Board': '1', 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
        cache: 'no-store',
      });
    } catch (error) {
      throw new Error('无法连接本地服务，请确认启动窗口仍然打开');
    }
    let data = null;
    try {
      data = await response.json();
    } catch { /* 非 JSON */ }
    if (!response.ok || !data || data.ok === false) {
      const message = data?.error?.message || `请求失败（HTTP ${response.status}）`;
      const err = new Error(message);
      err.code = data?.error?.code;
      err.status = response.status;
      throw err;
    }
    return data;
  }

  // ---------------------------------------------------------------- 汇总计算
  function summarize() {
    const list = Array.from(state.results.values());
    const byCurrency = new Map();
    let couponCount = 0;
    let expiringSoonCount = 0;
    let normal = 0;
    let partial = 0;
    let failed = 0;
    let nearest = null;

    for (const result of list) {
      const code = result.currency || 'CNY';
      const group = byCurrency.get(code) || {
        code, accounts: 0, available: 0, coupons: 0, total: 0,
      };
      group.accounts += 1;
      if (result.balance) group.available += num(result.balance.available);
      if (result.coupons) {
        group.coupons += num(result.coupons.availableBalance);
        couponCount += num(result.coupons.availableCount);
        expiringSoonCount += num(result.coupons.expiringSoonCount);
        const expiry = result.coupons.earliestExpiryTs;
        if (expiry && expiry >= Date.now() && (!nearest || expiry < nearest.ts)) {
          nearest = {
            ts: expiry,
            accountId: result.id,
            accountName: result.name,
            amount: result.coupons.availableBalance,
          };
        }
      }
      if (result.total !== null && result.total !== undefined) group.total += num(result.total);
      byCurrency.set(code, group);

      if (!result.ok) failed += 1;
      else if (result.errors && result.errors.length) partial += 1;
      else normal += 1;
    }

    const groups = Array.from(byCurrency.values()).sort((a, b) => {
      const weight = (g) => (g.code === 'CNY' ? 0 : 1);
      return weight(a) - weight(b) || b.total - a.total || a.code.localeCompare(b.code);
    });

    const hasBalance = list.some((result) => result.balance);
    const hasCoupons = list.some((result) => result.coupons);

    return {
      groups,
      primary: hasBalance ? groups[0] || null : null,
      hasBalance,
      hasCoupons,
      couponCount,
      expiringSoonCount,
      nearest,
      normal,
      partial,
      failed,
      queried: list.length,
      accounts: state.accounts.length,
    };
  }

  // ---------------------------------------------------------------- 渲染：汇总
  function setText(selector, text) {
    const el = $(selector);
    if (el) el.textContent = text;
  }

  function renderSummary() {
    const summary = summarize();
    const primary = summary.primary;
    const code = primary?.code || 'CNY';
    const querying = state.querying;

    setText('#heroCur', symbol(code));
    setText('#sumTotal', primary ? fmtMoney(primary.total) : '—');
    setText('#sumCoupons', primary ? amount(primary.coupons, code) : '—');
    setText('#sumAvailable', primary ? amount(primary.available, code) : '—');
    setText('#heroBadge', summary.groups.length > 1 ? '多币种' : '券 + 余额');

    setText('#cardCoupons', primary ? amount(primary.coupons, code) : '—');
    setText('#cardCouponSub', summary.hasCoupons
      ? `${summary.couponCount} 张可用${summary.expiringSoonCount ? ` · ${summary.expiringSoonCount} 张 30 天内到期` : ''}`
      : (summary.queried ? '代金券未读取' : '暂无数据'));

    const nearest = summary.nearest;
    setText('#cardExpiry', nearest ? fmtShort(nearest.ts) : '—');
    const expirySub = $('#cardExpirySub');
    setText('#cardExpirySub', nearest
      ? `${relativeExpiry(nearest.ts)} · ${nearest.accountName}`
      : '暂无可用券');
    if (expirySub) {
      expirySub.className = `stat-sub${nearest && daysUntil(nearest.ts) <= 30 ? ' warn' : ''}`;
    }

    setText('#cardAccounts', String(summary.accounts));
    setText('#cardAccountSub', summary.queried
      ? `${summary.normal} 正常${summary.partial ? ` · ${summary.partial} 部分` : ''}${summary.failed ? ` · ${summary.failed} 失败` : ''}`
      : '个账户');

    const hint = $('#sumHint');
    let hintText = '添加账户后开始查询';
    let alert = false;
    if (querying) {
      hintText = '正在从阿里云读取余额与代金券…';
    } else if (summary.queried) {
      hintText = `更新于 ${fmtTime(state.lastAt)} · 券与余额均为阿里云当前存量`;
      if (summary.failed) {
        hintText = `${summary.failed} 个账户查询失败，合计未包含其数据`;
        alert = true;
      } else if (summary.groups.length > 1) {
        const others = summary.groups.slice(1)
          .map((g) => `${symbol(g.code)}${fmtMoney(g.total)}`).join('、');
        hintText = `主币种 ${code} · 另有 ${others}`;
      }
    } else if (state.accounts.length) {
      hintText = '点击「刷新全部」开始查询';
    }
    if (hint) {
      hint.textContent = hintText;
      hint.className = `hero-hint${alert ? ' alert' : ''}`;
    }

    $('#lastUpdated').textContent = querying
      ? '正在查询…'
      : (state.lastAt ? `更新于 ${fmtTime(state.lastAt)}` : '尚未查询');

    const enabled = state.accounts.filter((a) => a.enabled).length;
    setText('#sectionSub', state.accounts.length ? `${summary.queried} / ${enabled} 已查询` : '');
    setText('#accountCount', String(state.accounts.length));
  }

  // ---------------------------------------------------------------- 渲染：表格
  function statusInfo(account, result) {
    if (!account.enabled) return { key: 'idle', label: '已停用' };
    if (state.querying && state.loading.has(account.id)) {
      return { key: 'loading', label: state.results.has(account.id) ? '刷新中' : '查询中' };
    }
    if (!result) return { key: 'idle', label: '未查询' };
    if (!result.ok) return { key: 'fail', label: '失败' };
    if (result.errors && result.errors.length) return { key: 'partial', label: '部分异常' };
    return { key: 'ok', label: '正常' };
  }

  function filteredAccounts() {
    const keyword = state.filter.trim().toLowerCase();
    if (!keyword) return state.accounts;
    return state.accounts.filter((a) => `${a.name} ${a.akMasked} ${a.akTail}`.toLowerCase().includes(keyword));
  }

  function rowHtml(account) {
    const result = state.results.get(account.id);
    const status = statusInfo(account, result);
    const code = result?.currency || 'CNY';
    const total = result && result.total !== null && result.total !== undefined ? amount(result.total, code) : '—';
    const available = result?.balance ? amount(result.balance.available, code) : '—';
    const coupons = result?.coupons ? amount(result.coupons.availableBalance, code) : '—';
    const couponCount = result?.coupons ? `${result.coupons.availableCount} 张` : '';
    const expiryTs = result?.coupons?.earliestExpiryTs;
    const error = result?.errors?.[0];

    const pill = status.key === 'loading'
      ? `<span class="pill idle loading"><span class="spinner"></span>${status.label}</span>`
      : `<span class="pill ${status.key}">${status.label}</span>`;

    return `
      <tr data-id="${esc(account.id)}" class="${account.enabled ? '' : 'row-disabled'}">
        <td>
          <div class="cell-account">
            <div class="avatar" style="${avatarStyle(account.name)}">${esc(initial(account.name))}</div>
            <div class="acct-meta">
              <div class="acct-name">${esc(account.name)}${account.enabled ? '' : '<span class="tag-off">停用</span>'}</div>
              <div class="acct-ak">${esc(account.akMasked)}</div>
            </div>
          </div>
        </td>
        <td class="num cell-total">${total}</td>
        <td class="num ${available === '—' ? 'cell-zero' : ''}">${available}</td>
        <td class="num ${coupons === '—' ? 'cell-zero' : ''}">${coupons}${couponCount ? `<span class="cell-sub">${couponCount}</span>` : ''}</td>
        <td>${expiryTs
          ? `<span class="expiry ${expiryClass(expiryTs)}">${fmtShort(expiryTs)}<span class="d"> · ${relativeExpiry(expiryTs)}</span></span>`
          : '<span class="expiry muted">—</span>'}</td>
        <td>
          ${pill}
          ${error ? `<span class="cell-error" title="${esc(`${friendlyError(error)}（${error.code}）`)}">${esc(shortError(error))}</span>` : ''}
        </td>
        <td>
          <div class="row-actions">
            <button class="icon-btn" type="button" data-act="refresh" title="刷新此账户" aria-label="刷新 ${esc(account.name)}">${icon('refresh')}</button>
            <button class="icon-btn" type="button" data-act="edit" title="编辑" aria-label="编辑 ${esc(account.name)}">${icon('edit')}</button>
            <button class="icon-btn" type="button" data-act="delete" title="删除" aria-label="删除 ${esc(account.name)}">${icon('trash')}</button>
          </div>
        </td>
      </tr>`;
  }

  function cardHtml(account) {
    const result = state.results.get(account.id);
    const status = statusInfo(account, result);
    const code = result?.currency || 'CNY';
    const total = result && result.total !== null && result.total !== undefined ? amount(result.total, code) : '—';
    const available = result?.balance ? amount(result.balance.available, code) : '—';
    const coupons = result?.coupons ? amount(result.coupons.availableBalance, code) : '—';
    const couponCount = result?.coupons ? `${result.coupons.availableCount} 张可用` : '';
    const expiryTs = result?.coupons?.earliestExpiryTs;
    const error = result?.errors?.[0];
    const pill = status.key === 'loading'
      ? `<span class="pill idle loading"><span class="spinner"></span>${status.label}</span>`
      : `<span class="pill ${status.key}">${status.label}</span>`;

    return `
      <article class="acct-card ${account.enabled ? '' : 'row-disabled'}" data-id="${esc(account.id)}">
        <div class="acct-card-head">
          <div class="avatar" style="${avatarStyle(account.name)}">${esc(initial(account.name))}</div>
          <div class="acct-meta">
            <div class="acct-name">${esc(account.name)}${account.enabled ? '' : '<span class="tag-off">停用</span>'}</div>
            <div class="acct-ak">${esc(account.akMasked)}</div>
          </div>
          ${pill}
        </div>
        <div class="acct-card-metrics">
          <div><span>可用合计</span><b>${total}</b></div>
          <div><span>账户余额</span><b>${available}</b></div>
          <div><span>代金券</span><b>${coupons}</b><small>${couponCount}</small></div>
          <div><span>最近到期</span><b class="${expiryTs ? expiryClass(expiryTs) : ''}">${expiryTs ? fmtShort(expiryTs) : '—'}</b><small>${expiryTs ? relativeExpiry(expiryTs) : ''}</small></div>
        </div>
        ${error ? `<div class="acct-card-error">${esc(shortError(error))}</div>` : ''}
        <div class="acct-card-foot">
          <span>${result ? `更新于 ${fmtTime(result.fetchedAt)}` : '尚未查询'}</span>
          <div class="row-actions">
            <button class="icon-btn" type="button" data-act="refresh" title="刷新此账户" aria-label="刷新 ${esc(account.name)}">${icon('refresh')}</button>
            <button class="icon-btn" type="button" data-act="edit" title="编辑" aria-label="编辑 ${esc(account.name)}">${icon('edit')}</button>
            <button class="icon-btn" type="button" data-act="delete" title="删除" aria-label="删除 ${esc(account.name)}">${icon('trash')}</button>
          </div>
        </div>
      </article>`;
  }

  function renderTable() {
    const accounts = filteredAccounts();
    const hasAccounts = state.accounts.length > 0;
    const noMatch = hasAccounts && accounts.length === 0;
    $('#emptyState').hidden = hasAccounts;
    $('#tableWrap').hidden = !hasAccounts || noMatch;
    $('#accountCards').hidden = !hasAccounts || noMatch;
    $('#noMatch').hidden = !noMatch;
    $('#accountRows').innerHTML = accounts.map(rowHtml).join('');
    $('#accountCards').innerHTML = accounts.map(cardHtml).join('');
    $('.section-tools').hidden = !hasAccounts;
    $('#searchInput').disabled = !hasAccounts;
    $('#queryBar').hidden = !state.querying;
    $('#btnRefresh').disabled = state.querying || !hasAccounts;
    $('#btnRefresh').classList.toggle('busy', state.querying);
    if (state.querying) {
      setText('#queryText', `正在查询 ${state.loading.size} 个账户…`);
    }
    setText('#accountCount', String(state.accounts.length));
    $('#accountCount').hidden = !hasAccounts;
  }

  // ---------------------------------------------------------------- 渲染：挂件
  function renderWidget() {
    const widget = $('#widget');
    widget.hidden = !state.settings.widget;
    if (!state.settings.widget) return;

    const summary = summarize();
    const primary = summary.primary;
    const code = primary?.code || 'CNY';
    const totalText = primary ? `${symbol(code)}${fmtMoney(primary.total)}` : '—';

    setText('#widgetAmount', totalText);
    setText('#widgetCur', symbol(code));
    setText('#widgetTotal', primary ? fmtMoney(primary.total) : '—');
    setText('#widgetCoupons', primary ? amount(primary.coupons, code) : '—');
    setText('#widgetAvailable', primary ? amount(primary.available, code) : '—');
    setText('#widgetExpiry', summary.nearest ? `${fmtShort(summary.nearest.ts)}（${daysUntil(summary.nearest.ts)} 天）` : '—');
    setText('#widgetAccounts', summary.queried ? `${summary.normal}/${summary.accounts}` : '—');
    setText('#widgetTime', state.querying ? '查询中…' : (state.lastAt ? fmtTime(state.lastAt) : '未查询'));

    const dot = $('#widgetDot');
    dot.className = 'status-dot';
    if (state.querying) dot.classList.add('idle');
    else if (summary.queried && summary.failed === summary.queried) dot.classList.add('fail');
    else if (summary.failed) dot.classList.add('partial');
    else if (summary.queried) dot.classList.add('ok');

    $('#widgetPill').title = `可用 ${totalText} · 单击刷新，拖动移动，双击复位`;
  }

  function renderAll() {
    renderSummary();
    renderTable();
    renderWidget();
  }

  // ---------------------------------------------------------------- 渲染：明细
  function couponRow(item) {
    const status = COUPON_STATUS[String(item.status || '').toLowerCase()] || { label: item.status || '—', cls: 'muted' };
    const expiry = item.expiryTs ? `${fmtDate(item.expiryTs)}` : (item.expiryTime || '—');
    const rel = item.expiryTs ? relativeExpiry(item.expiryTs) : '';
    const cls = item.expiryTs ? expiryClass(item.expiryTs) : 'muted';
    return `
      <tr>
        <td>
          <div class="coupon-name">${esc(item.description || item.applicableProducts || '代金券')}</div>
          <div class="coupon-desc">${esc(item.no || item.id || '')}${item.applicableProducts && item.description ? ` · ${esc(item.applicableProducts)}` : ''}</div>
        </td>
        <td class="r">${amount(item.nominalValue)}</td>
        <td class="r"><b>${amount(item.balance)}</b></td>
        <td><span class="expiry ${status.cls === 'ok' ? '' : status.cls}">${esc(status.label)}</span></td>
        <td class="r"><span class="expiry ${cls}">${esc(expiry)}${rel ? `<div class="coupon-desc">${esc(rel)}</div>` : ''}</span></td>
      </tr>`;
  }

  function detailHtml(account, result) {
    const status = statusInfo(account, result);
    const code = result?.currency || 'CNY';
    const metrics = `
      <div class="detail-grid">
        <div class="detail-metric"><span>可用合计</span><strong>${result && result.total !== null ? amount(result.total, code) : '—'}</strong><small>代金券 + 账户余额</small></div>
        <div class="detail-metric"><span>账户余额</span><strong>${result?.balance ? amount(result.balance.available, code) : '—'}</strong><small>阿里云可用额度，不含券</small></div>
        <div class="detail-metric"><span>代金券余额</span><strong>${result?.coupons ? amount(result.coupons.availableBalance, code) : '—'}</strong><small>${result?.coupons ? `${result.coupons.availableCount} 张可用` : '未读取'}</small></div>
      </div>`;

    const facts = result?.balance ? `
      <div class="detail-facts">
        <span>现金余额<b>${amount(result.balance.cash, code)}</b></span>
        <span>信控额度<b>${amount(result.balance.credit, code)}</b></span>
        ${result.balance.mybankCredit !== null && result.balance.mybankCredit !== undefined ? `<span>网商额度<b>${amount(result.balance.mybankCredit, code)}</b></span>` : ''}
        <span>币种<b>${esc(code)}</b></span>
        <span>耗时<b>${result.latencyMs ?? '—'} ms</b></span>
      </div>` : '';

    let couponsBlock = '';
    if (!result) {
      couponsBlock = '<div class="detail-empty">这个账户还没有查询过，点下方「刷新此账户」。</div>';
    } else if (!result.coupons) {
      couponsBlock = '<div class="detail-empty">代金券信息未能读取，原因见下方错误。</div>';
    } else if (!result.coupons.items.length) {
      couponsBlock = '<div class="detail-empty">这个账户名下没有代金券。</div>';
    } else {
      couponsBlock = `
        <div class="table-wrap">
          <table class="coupon-table">
            <thead><tr><th>券</th><th class="r">面值</th><th class="r">余额</th><th>状态</th><th class="r">失效时间</th></tr></thead>
            <tbody>${result.coupons.items.map(couponRow).join('')}</tbody>
          </table>
        </div>`;
    }

    const errors = (result?.errors || []).map((err) => `
      <div class="detail-error">
        ${icon('alert')}
        <div>
          <b>${err.scope === 'balance' ? '账户余额' : (err.scope === 'coupons' ? '代金券' : '查询')}失败</b>
          <p>${esc(friendlyError(err))}<br><code>${esc(err.code || 'Error')}</code> ${esc(String(err.message || '').slice(0, 240))}</p>
        </div>
      </div>`).join('');

    return `
      <div class="detail-top">
        <div class="avatar" style="${avatarStyle(account.name)}">${esc(initial(account.name))}</div>
        <div>
          <div class="acct-name">${esc(account.name)}</div>
          <div class="acct-ak">${esc(account.akMasked)} · ${esc(account.endpoint)}</div>
        </div>
        <span class="pill ${status.key}">${status.label}</span>
        <span class="section-sub" style="margin-left:auto">${result ? `更新于 ${fmtTime(result.fetchedAt)} · ${result.latencyMs ?? '—'} ms` : '尚未查询'}</span>
      </div>
      ${metrics}
      ${facts}
      <div class="detail-section-title">代金券
        ${result?.coupons ? `<span class="pill ${result.coupons.availableCount ? 'ok' : 'idle'}">${result.coupons.availableCount} 张可用</span>` : ''}
        ${result?.coupons?.expiringSoonCount ? `<span class="pill partial">${result.coupons.expiringSoonCount} 张 30 天内到期</span>` : ''}
      </div>
      ${couponsBlock}
      ${errors}`;
  }

  function openDetail(accountId) {
    const account = state.accounts.find((a) => a.id === accountId);
    if (!account) return;
    const result = state.results.get(accountId);
    $('#detailTitle').textContent = '账户明细';
    $('#detailSub').textContent = `${account.akMasked} · ${account.endpoint}`;
    $('#detailBody').innerHTML = detailHtml(account, result);
    $('#detailDialog').dataset.id = accountId;
    $('#detailDialog').showModal();
  }

  function refreshOpenDetail() {
    const dialog = $('#detailDialog');
    if (!dialog.open) return;
    const accountId = dialog.dataset.id;
    const account = state.accounts.find((a) => a.id === accountId);
    if (!account) {
      dialog.close();
      return;
    }
    $('#detailBody').innerHTML = detailHtml(account, state.results.get(accountId));
  }

  // ---------------------------------------------------------------- 账户弹窗
  function switchTab(tab) {
    state.accountTab = tab;
    $$('#accountTabs .tab').forEach((el) => {
      const active = el.dataset.tab === tab;
      el.classList.toggle('active', active);
      el.setAttribute('aria-selected', String(active));
    });
    $('#singlePane').hidden = tab !== 'single';
    $('#batchPane').hidden = tab !== 'batch';
    $('#btnTest').style.visibility = tab === 'single' ? 'visible' : 'hidden';
    if (tab === 'batch') updateBatchPreview();
  }

  function openAccountDialog({ mode = 'add', account = null, tab = 'single' } = {}) {
    state.editingId = account?.id || null;
    const form = $('#accountForm');
    form.reset();
    $('#testResult').hidden = true;
    $('#testResult').className = 'test-result';
    $('#fEndpoint').value = account?.endpoint || DEFAULT_ENDPOINT;
    $('#advancedBox').open = Boolean(account && account.endpoint && account.endpoint !== DEFAULT_ENDPOINT);
    $('#enabledRow').hidden = !account;
    $('#fEnabled').checked = account ? account.enabled : true;
    $('#accountTabs').hidden = Boolean(account);
    $('#accountDialogTitle').textContent = account ? '编辑账户' : '添加账户';
    $('#accountDialogSub').textContent = account
      ? '留空 Secret 表示保持原值不变'
      : '密钥只保存在本机，不会上传到任何服务器';
    $('#btnSave').textContent = account ? '保存修改' : '添加';
    $('#fSk').placeholder = account ? '留空则保持原 Secret 不变' : '只读子账号的 Secret';
    $('#fSk').type = 'password';
    $('#toggleSk use').setAttribute('href', '#i-eye');
    $('#batchText').value = '';
    $('#batchPreview').textContent = '';
    $('#batchErrors').hidden = true;
    $('#batchErrors').innerHTML = '';

    if (account) {
      $('#fName').value = account.name || '';
      $('#fAk').value = account.akMasked;
      // 编辑时输入框里是掩码，服务端不会把它当 AK 用；用户不改动则提交原值
      $('#fAk').dataset.masked = '1';
      $('#fAk').title = '当前 AK：' + account.akMasked + '（如需更换请整体替换）';
    } else {
      delete $('#fAk').dataset.masked;
      $('#fAk').title = '';
    }

    switchTab(tab);
    $('#accountDialog').showModal();
    setTimeout(() => {
      if (account) $('#fAk').focus();
      else $('#fName').focus();
    }, 30);
  }

  function setBusy(button, busy, busyText) {
    if (!button) return;
    if (busy) {
      button.dataset.label = button.innerHTML;
      button.disabled = true;
      button.innerHTML = `<span class="spinner"></span><span>${esc(busyText || '处理中…')}</span>`;
    } else {
      button.disabled = false;
      if (button.dataset.label) button.innerHTML = button.dataset.label;
    }
  }

  // ---------------------------------------------------------------- 批量解析
  function looksAk(value) {
    return /^LTAI[0-9A-Za-z]{6,}$/.test(value) || /^STS\.[0-9A-Za-z._-]{8,}$/.test(value);
  }

  function looksEndpoint(value) {
    return /^[a-z0-9.-]+\.aliyuncs\.com$/i.test(value);
  }

  function splitFields(line) {
    if (line.includes('\t')) return line.split('\t');
    if (line.includes(',')) return line.split(',');
    if (line.includes('，')) return line.split('，');
    return line.split(/\s+/);
  }

  function parseBatch(text) {
    const rows = [];
    const errors = [];
    const warnings = [];
    const seen = new Map();
    const raw = String(text || '').trim();
    if (!raw) return { rows, errors, warnings };

    const addRow = (name, ak, sk, endpoint, lineNo) => {
      ak = String(ak || '').trim();
      sk = String(sk || '').trim();
      endpoint = String(endpoint || '').trim() || DEFAULT_ENDPOINT;
      name = String(name || '').trim();
      if (!ak || ak.length < 8 || /\s/.test(ak)) {
        errors.push({ line: lineNo, message: 'AccessKey ID 格式不正确' });
        return;
      }
      if (!sk || sk.length < 8 || /\s/.test(sk)) {
        errors.push({ line: lineNo, message: 'AccessKey Secret 格式不正确' });
        return;
      }
      if (looksEndpoint(name)) {
        endpoint = name;
        name = '';
      }
      if (seen.has(ak)) {
        warnings.push(`第 ${lineNo} 行与第 ${seen.get(ak)} 行 AK 重复，已跳过`);
        return;
      }
      if (!looksEndpoint(endpoint)) {
        errors.push({ line: lineNo, message: `Endpoint 不合法：${endpoint}` });
        return;
      }
      seen.set(ak, lineNo);
      rows.push({ name, ak, sk, endpoint });
    };

    if (raw.startsWith('[') || raw.startsWith('{')) {
      let data;
      try {
        data = JSON.parse(raw);
      } catch (error) {
        errors.push({ line: 0, message: `JSON 解析失败：${error.message}` });
        return { rows, errors, warnings };
      }
      const list = Array.isArray(data) ? data : (Array.isArray(data.accounts) ? data.accounts : [data]);
      list.forEach((item, index) => {
        if (!item || typeof item !== 'object') {
          errors.push({ line: index + 1, message: '不是账户对象' });
          return;
        }
        addRow(
          item.name || item.Name || item.remark || '',
          item.ak || item.AK || item.accessKeyId || item.AccessKeyId || '',
          item.sk || item.SK || item.accessKeySecret || item.AccessKeySecret || '',
          item.endpoint || item.Endpoint || '',
          index + 1,
        );
      });
      return { rows, errors, warnings };
    }

    raw.split(/\r?\n/).forEach((line, index) => {
      const lineNo = index + 1;
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith('#')) return;
      let fields = splitFields(trimmed).map((field) => field.trim());
      while (fields.length && fields[fields.length - 1] === '') fields.pop();

      let name = '';
      let ak = '';
      let sk = '';
      let endpoint = '';

      if (fields.length === 2) {
        [ak, sk] = fields;
      } else if (fields.length >= 3) {
        if (looksAk(fields[0])) {
          [ak, sk] = fields;
          const extra = fields.slice(2);
          if (extra[0] && looksEndpoint(extra[0])) endpoint = extra[0];
          else if (extra[0]) name = extra[0];
          if (extra[1] && looksEndpoint(extra[1])) endpoint = extra[1];
        } else if (looksAk(fields[1])) {
          [name, ak, sk] = fields;
          endpoint = fields[3] || '';
        } else {
          [ak, sk] = fields;
          const extra = fields.slice(2);
          if (extra[0] && looksEndpoint(extra[0])) endpoint = extra[0];
          else if (extra[0]) name = extra[0];
        }
      } else {
        errors.push({ line: lineNo, message: '至少需要 AK 和 SK 两列' });
        return;
      }
      addRow(name, ak, sk, endpoint, lineNo);
    });

    return { rows, errors, warnings };
  }

  function updateBatchPreview() {
    const text = $('#batchText').value;
    const preview = $('#batchPreview');
    const errorsBox = $('#batchErrors');
    if (!text.trim()) {
      preview.textContent = '';
      errorsBox.hidden = true;
      errorsBox.innerHTML = '';
      return;
    }
    const { rows, errors, warnings } = parseBatch(text);
    if (!rows.length && !errors.length && !warnings.length) {
      preview.textContent = '未识别到账户。每行至少填写 AK 和 SK。';
    } else {
      const names = rows.slice(0, 4).map((row) => esc(row.name || `账户 ${row.ak.slice(-4)}`)).join('、');
      preview.innerHTML = `识别到 <b>${rows.length}</b> 个账户${rows.length ? `：${names}${rows.length > 4 ? ' 等' : ''}` : ''}`;
    }
    errorsBox.hidden = !(errors.length || warnings.length);
    errorsBox.innerHTML = [
      ...errors.slice(0, 6).map((err) => `<div>${err.line ? `第 ${err.line} 行：` : ''}${esc(err.message)}</div>`),
      ...warnings.slice(0, 4).map((msg) => `<div>${esc(msg)}</div>`),
    ].join('');
  }

  // ---------------------------------------------------------------- 查询
  function mergeAccounts(accounts) {
    state.accounts = accounts;
    const ids = new Set(accounts.map((a) => a.id));
    for (const key of Array.from(state.results.keys())) {
      if (!ids.has(key)) state.results.delete(key);
    }
  }

  async function reloadAccounts() {
    const data = await api('/api/accounts');
    mergeAccounts(data.accounts || []);
    renderAll();
  }

  async function queryAccounts(ids = null, options = {}) {
    if (state.demo) {
      if (!options.silent) toast('演示模式：示例数据保持不变', 'ok');
      return;
    }
    if (state.querying) return;
    const targets = ids || state.accounts.filter((a) => a.enabled).map((a) => a.id);
    if (!targets.length) {
      if (!options.silent) toast('没有可查询的账户', 'err');
      return;
    }

    state.querying = true;
    state.loading = new Set(targets);
    if (ids) state.loading = new Set(ids);
    renderAll();

    try {
      const data = await api('/api/query', { method: 'POST', body: { ids: targets } });
      for (const result of data.results || []) state.results.set(result.id, result);
      state.lastAt = data.at || Date.now();
      if (ids && ids.length === 1 && !options.silent) {
        const result = data.results?.[0];
        const error = result?.errors?.[0];
        if (error) toast(friendlyError(error), 'err');
      }
    } catch (error) {
      if (!options.silent) toast(error.message, 'err');
    } finally {
      state.querying = false;
      state.loading.clear();
      renderAll();
      refreshOpenDetail();
    }
  }

  // ---------------------------------------------------------------- 测试连接
  function renderTestResult(result) {
    const box = $('#testResult');
    box.hidden = false;
    if (result.ok) {
      const code = result.currency || 'CNY';
      box.className = 'test-result ok';
      box.innerHTML = `<b>连接成功</b>
        <div class="tr-lines">
          <span>可用合计 <b>${amount(result.total, code)}</b>（券 ${amount(result.coupons?.availableBalance, code)} + 余额 ${amount(result.balance?.available, code)}）</span>
          <span>代金券 ${result.coupons?.availableCount ?? 0} 张可用${result.coupons?.earliestExpiryTs ? ` · 最近到期 ${fmtDate(result.coupons.earliestExpiryTs)}` : ''}</span>
        </div>`;
    } else {
      box.className = 'test-result err';
      const lines = (result.errors || []).map((err) => `<span>${esc(friendlyError(err))}（<code>${esc(err.code || 'Error')}</code>）</span>`).join('');
      box.innerHTML = `<b>连接失败</b><div class="tr-lines">${lines || '<span>未知错误</span>'}</div>`;
    }
  }

  async function testConnection() {
    if (state.demo) {
      toast('演示模式：不发起真实连接', 'err');
      return;
    }
    const ak = $('#fAk').value.trim();
    const sk = $('#fSk').value.trim();
    const endpoint = $('#fEndpoint').value.trim() || DEFAULT_ENDPOINT;
    if (!state.editingId && (!ak || !sk)) {
      toast('请先填写 AK 和 SK', 'err');
      return;
    }
    if (!ak || ak.includes('****')) {
      if (!state.editingId) {
        toast('请填写 AccessKey ID', 'err');
        return;
      }
    }
    const button = $('#btnTest');
    setBusy(button, true, '测试中…');
    try {
      const body = { endpoint };
      if (state.editingId) body.id = state.editingId;
      if (ak && !ak.includes('****')) body.ak = ak;
      if (sk) body.sk = sk;
      const data = await api('/api/test', { method: 'POST', body });
      renderTestResult(data.result);
    } catch (error) {
      const box = $('#testResult');
      box.hidden = false;
      box.className = 'test-result err';
      box.innerHTML = `<b>测试失败</b><div class="tr-lines"><span>${esc(error.message)}</span></div>`;
    } finally {
      setBusy(button, false);
    }
  }

  // ---------------------------------------------------------------- 保存账户
  async function submitSingle() {
    const name = $('#fName').value.trim();
    const ak = $('#fAk').value.trim();
    const sk = $('#fSk').value.trim();
    const endpoint = $('#fEndpoint').value.trim() || DEFAULT_ENDPOINT;
    const enabled = state.editingId ? $('#fEnabled').checked : true;

    if (!state.editingId && !sk) {
      toast('请填写 AccessKey Secret', 'err');
      $('#fSk').focus();
      return;
    }
    if (!ak) {
      toast('请填写 AccessKey ID', 'err');
      $('#fAk').focus();
      return;
    }

    const button = $('#btnSave');
    setBusy(button, true, '保存中…');
    try {
      if (state.editingId) {
        const body = { name, endpoint, enabled };
        if (!(ak.includes('****'))) body.ak = ak;
        if (sk) body.sk = sk;
        await api(`/api/accounts/${state.editingId}`, { method: 'PUT', body });
        state.results.delete(state.editingId);
        $('#accountDialog').close();
        await reloadAccounts();
        toast('已保存修改', 'ok');
        if (enabled) queryAccounts([state.editingId]);
      } else {
        const data = await api('/api/accounts', {
          method: 'POST',
          body: { accounts: [{ name, ak, sk, endpoint, enabled }] },
        });
        if (data.errors?.length) {
          toast(data.errors[0].message || '添加失败', 'err');
          return;
        }
        $('#accountDialog').close();
        await reloadAccounts();
        toast('账户已添加，开始查询…', 'ok');
        queryAccounts();
      }
    } catch (error) {
      toast(error.message, 'err');
    } finally {
      setBusy(button, false);
    }
  }

  async function submitBatch() {
    const { rows, errors, warnings } = parseBatch($('#batchText').value);
    const box = $('#batchErrors');
    if (!rows.length) {
      box.hidden = false;
      box.innerHTML = (errors.length ? errors.slice(0, 8).map((err) => `<div>${err.line ? `第 ${err.line} 行：` : ''}${esc(err.message)}</div>`).join('') : '没有识别到有效账户');
      return;
    }
    if (warnings.length) toast(warnings[0], 'ok');

    const button = $('#btnSave');
    setBusy(button, true, '导入中…');
    try {
      const data = await api('/api/accounts', { method: 'POST', body: { accounts: rows } });
      const failedRows = data.errors || [];
      if (!data.added && !data.updated && failedRows.length) {
        box.hidden = false;
        box.innerHTML = failedRows.slice(0, 8).map((err) => `<div>第 ${err.index + 1} 个：${esc(err.message)}</div>`).join('');
        toast('没有账户被导入，请检查填写内容', 'err');
        return;
      }
      $('#accountDialog').close();
      await reloadAccounts();
      if (failedRows.length) {
        toast(`新增 ${data.added}，更新 ${data.updated}；${failedRows.length} 行失败`, 'err');
      } else {
        toast(`新增 ${data.added} 个账户${data.updated ? `，更新 ${data.updated} 个` : ''}`, 'ok');
      }
      if (data.added || data.updated) queryAccounts();
    } catch (error) {
      toast(error.message, 'err');
    } finally {
      setBusy(button, false);
    }
  }

  async function onAccountSubmit(event) {
    event.preventDefault();
    if (state.demo) {
      toast('演示模式：不会写入账户', 'err');
      return;
    }
    if (state.accountTab === 'batch' && !state.editingId) await submitBatch();
    else await submitSingle();
  }

  // ---------------------------------------------------------------- 删除 / 清空
  function askConfirm({ title, text, okText = '确认', danger = true }, handler) {
    $('#confirmTitle').textContent = title;
    $('#confirmText').textContent = text;
    const ok = $('#confirmOk');
    ok.textContent = okText;
    ok.className = `btn ${danger ? 'danger' : 'primary'}`;
    confirmHandler = handler;
    $('#confirmDialog').showModal();
  }

  async function deleteAccount(accountId) {
    const account = state.accounts.find((a) => a.id === accountId);
    if (!account) return;
    askConfirm({
      title: '删除账户',
      text: `确定删除「${account.name}」？本机保存的 AK / SK 会一并删除，无法恢复。`,
      okText: '删除',
    }, async () => {
      await api(`/api/accounts/${accountId}`, { method: 'DELETE' });
      state.results.delete(accountId);
      state.accounts = state.accounts.filter((a) => a.id !== accountId);
      renderAll();
      toast('已删除', 'ok');
    });
  }

  function clearAll() {
    if (state.demo) {
      toast('演示模式：没有可清空的账户', 'err');
      return;
    }
    if (!state.accounts.length) {
      toast('当前没有账户', 'err');
      return;
    }
    askConfirm({
      title: '清空所有账户',
      text: `将从本机删除 ${state.accounts.length} 个账户的 AK / SK，无法恢复。`,
      okText: '全部清空',
    }, async () => {
      await api('/api/accounts/clear', { method: 'POST' });
      state.accounts = [];
      state.results.clear();
      state.lastAt = 0;
      renderAll();
      $('#settingsDialog').close();
      toast('已清空', 'ok');
    });
  }

  // ---------------------------------------------------------------- 设置
  function syncSettings() {
    $('#setAuto').value = String(state.settings.autoMin);
    $('#setWidget').checked = state.settings.widget;
    $('#setTheme').value = state.theme;
    const secure = state.protection === 'dpapi';
    $('#securityTitle').textContent = secure ? 'Windows DPAPI 加密' : '未使用 DPAPI';
    $('#securityText').textContent = secure
      ? 'Secret 使用当前 Windows 用户的 DPAPI 加密后保存在本机 data/accounts.json，文件被拷走也无法解密。界面与接口都不会回显 SK。'
      : '当前环境无法使用 Windows DPAPI，Secret 以 base64 混淆保存。请勿在共享电脑上保存重要凭据。';
  }

  function openSettings() {
    syncSettings();
    $('#settingsDialog').showModal();
  }

  function setupAutoRefresh() {
    clearInterval(autoTimer);
    autoTimer = null;
    const minutes = Number(state.settings.autoMin) || 0;
    if (minutes <= 0) return;
    autoTimer = setInterval(() => {
      if (!document.hidden && state.accounts.some((a) => a.enabled)) {
        queryAccounts(null, { silent: true });
      }
    }, minutes * 60000);
  }

  // ---------------------------------------------------------------- 导出
  function csvCell(value) {
    const text = String(value ?? '');
    return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  }

  function download(filename, content, mime) {
    const blob = new Blob([content], { type: mime });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1500);
  }

  function stamp() {
    const d = new Date();
    return `${d.getFullYear()}${pad2(d.getMonth() + 1)}${pad2(d.getDate())}-${pad2(d.getHours())}${pad2(d.getMinutes())}`;
  }

  function exportCsv() {
    if (!state.accounts.length) {
      toast('还没有账户可导出', 'err');
      return;
    }
    const header = ['账户', 'AccessKey', 'Endpoint', '币种', '账户余额', '代金券余额', '可用合计', '可用券数', '最近到期', '状态', '错误', '查询时间'];
    const lines = [header.map(csvCell).join(',')];
    for (const account of state.accounts) {
      const result = state.results.get(account.id);
      const status = statusInfo(account, result);
      lines.push([
        account.name,
        account.akMasked,
        account.endpoint,
        result?.currency || 'CNY',
        result?.balance?.available ?? '',
        result?.coupons?.availableBalance ?? '',
        result?.total ?? '',
        result?.coupons?.availableCount ?? '',
        result?.coupons?.earliestExpiryTs ? fmtDate(result.coupons.earliestExpiryTs) : '',
        status.label,
        result?.errors?.map((err) => `${err.code}: ${err.message}`).join(' | ') || '',
        result?.fetchedAt ? fmtDate(result.fetchedAt) + ' ' + fmtTime(result.fetchedAt) : '',
      ].map(csvCell).join(','));
    }
    download(`aliyun-balance-${stamp()}.csv`, '\ufeff' + lines.join('\r\n'), 'text/csv;charset=utf-8');
    toast('已导出 CSV', 'ok');
  }

  function exportJson() {
    if (!state.accounts.length) {
      toast('还没有账户可导出', 'err');
      return;
    }
    const summary = summarize();
    const payload = {
      app: 'aliyun-balance-board',
      exportedAt: new Date().toISOString(),
      summary: {
        accounts: summary.accounts,
        normal: summary.normal,
        partial: summary.partial,
        failed: summary.failed,
        byCurrency: summary.groups,
      },
      accounts: state.accounts.map((account) => ({
        ...account,
        result: state.results.get(account.id) || null,
      })),
    };
    download(`aliyun-balance-${stamp()}.json`, JSON.stringify(payload, null, 2), 'application/json;charset=utf-8');
    toast('已导出 JSON', 'ok');
  }

  async function copySummary() {
    const summary = summarize();
    const primary = summary.primary;
    if (!summary.queried) {
      toast('还没有可复制的数据', 'err');
      return;
    }
    const lines = [
      `阿里云余额批量看板 · ${fmtDate(Date.now())} ${fmtTime(state.lastAt)}`,
      `可用合计：${primary ? amount(primary.total, primary.code) : '—'}`,
      `代金券：${primary ? amount(primary.coupons, primary.code) : '—'}（${summary.couponCount} 张可用）`,
      `账户余额：${primary ? amount(primary.available, primary.code) : '—'}`,
      `最近到期：${summary.nearest ? `${fmtDate(summary.nearest.ts)}（${relativeExpiry(summary.nearest.ts)}）· ${summary.nearest.accountName}` : '无可用代金券'}`,
      `账户：${summary.accounts} 个（${summary.normal} 正常${summary.failed ? `，${summary.failed} 失败` : ''}）`,
    ];
    try {
      await navigator.clipboard.writeText(lines.join('\n'));
      toast('汇总已复制', 'ok');
    } catch {
      const area = document.createElement('textarea');
      area.value = lines.join('\n');
      document.body.appendChild(area);
      area.select();
      document.execCommand('copy');
      area.remove();
      toast('汇总已复制', 'ok');
    }
  }

  // ---------------------------------------------------------------- 悬浮挂件
  function positionWidget(x, y) {
    const widget = $('#widget');
    const maxX = Math.max(8, window.innerWidth - widget.offsetWidth - 8);
    const maxY = Math.max(8, window.innerHeight - widget.offsetHeight - 8);
    widget.classList.add('positioned');
    widget.style.left = `${clamp(x, 8, maxX)}px`;
    widget.style.top = `${clamp(y, 8, maxY)}px`;
    const rect = widget.getBoundingClientRect();
    widget.classList.toggle('pop-left', rect.left < 150);
    widget.classList.toggle('pop-bottom', rect.top < 250);
  }

  function saveWidgetPosition() {
    const widget = $('#widget');
    if (!widget.classList.contains('positioned')) return;
    const rect = widget.getBoundingClientRect();
    storeSet(LS.pos, { x: Math.round(rect.left), y: Math.round(rect.top) });
  }

  function resetWidget() {
    const widget = $('#widget');
    widget.classList.remove('positioned', 'pop-left', 'pop-bottom');
    widget.style.left = '';
    widget.style.top = '';
    try { localStorage.removeItem(LS.pos); } catch { /* ignore */ }
    toast('挂件位置已复位', 'ok');
  }

  function initWidget() {
    const widget = $('#widget');
    const pill = $('#widgetPill');
    const saved = storeGet(LS.pos, null);
    if (saved && Number.isFinite(saved.x) && Number.isFinite(saved.y)) {
      positionWidget(saved.x, saved.y);
    }

    let drag = null;
    pill.addEventListener('pointerdown', (event) => {
      if (event.button !== 0) return;
      if (event.target.closest('#widgetMenu')) return;
      const rect = widget.getBoundingClientRect();
      drag = {
        startX: event.clientX,
        startY: event.clientY,
        left: rect.left,
        top: rect.top,
        moved: false,
        pointerId: event.pointerId,
      };
      try { pill.setPointerCapture(event.pointerId); } catch { /* ignore */ }
    });

    pill.addEventListener('pointermove', (event) => {
      if (!drag) return;
      const dx = event.clientX - drag.startX;
      const dy = event.clientY - drag.startY;
      if (!drag.moved && Math.hypot(dx, dy) > 5) {
        drag.moved = true;
        widget.classList.add('dragging');
      }
      if (!drag.moved) return;
      positionWidget(drag.left + dx, drag.top + dy);
    });

    const finishDrag = () => {
      if (!drag) return;
      const moved = drag.moved;
      drag = null;
      widget.classList.remove('dragging');
      if (moved) {
        saveWidgetPosition();
        suppressWidgetUntil = Date.now() + 400;
      }
    };
    pill.addEventListener('pointerup', finishDrag);
    pill.addEventListener('pointercancel', finishDrag);

    pill.addEventListener('click', (event) => {
      if (event.target.closest('#widgetMenu')) {
        event.stopPropagation();
        openSettings();
        return;
      }
      if (suppressWidgetUntil && Date.now() < suppressWidgetUntil) {
        suppressWidgetUntil = 0;
        return;
      }
      clearTimeout(clickTimer);
      clickTimer = setTimeout(() => queryAccounts(), 250);
    });

    pill.addEventListener('dblclick', (event) => {
      event.preventDefault();
      clearTimeout(clickTimer);
      resetWidget();
    });

    $('#widgetMenu').addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        openSettings();
      }
    });

    window.addEventListener('resize', () => {
      if (!widget.classList.contains('positioned')) return;
      const rect = widget.getBoundingClientRect();
      positionWidget(rect.left, rect.top);
    });

    // 双击空白处不触发两次查询：单双击都通过上面的定时器节流
  }

  // ---------------------------------------------------------------- 演示数据
  function seedDemo() {
    const now = Date.now();
    const day = 86400000;
    const coupon = (id, nominal, balance, status, expiryDays, desc) => ({
      id,
      no: `CP${id}`.toUpperCase(),
      nominalValue: nominal,
      balance,
      status,
      effectiveTime: new Date(now - 30 * day).toISOString(),
      expiryTime: new Date(now + expiryDays * day).toISOString(),
      expiryTs: now + expiryDays * day,
      grantedTime: new Date(now - 30 * day).toISOString(),
      applicableProducts: '通用',
      applicableScenarios: '',
      description: desc,
      available: status === 'Available',
    });

    const account = (id, name, tail, enabled = true) => ({
      id,
      name,
      akMasked: `LTAI****${tail}`,
      akTail: tail,
      hasSk: true,
      endpoint: DEFAULT_ENDPOINT,
      enabled,
      createdAt: now - 12 * day,
      updatedAt: now - day,
    });

    state.accounts = [
      account('demo00000001', '云工开物 · 学生号', '9K2M'),
      account('demo00000002', '实验室公用账号', '7Q4A'),
      account('demo00000003', '千问 API · 测试', '3F8D'),
      account('demo00000004', '毕设备用号', '5T1X'),
    ];

    const fetchedAt = now - 32000;
    const studentCoupons = [
      coupon('c1001', '300.00', '158.50', 'Available', 18, '云工开物高校学生专属代金券'),
      coupon('c1002', '50.00', '0.00', 'Used', -20, '新用户通用代金券'),
    ];
    const labCoupons = [
      coupon('c2001', '50.00', '50.00', 'Available', 95, '实验室算力补贴券'),
      coupon('c2002', '20.00', '12.50', 'Available', 46, 'ECS 通用代金券'),
    ];

    state.results = new Map([
      ['demo00000001', {
        id: 'demo00000001',
        name: '云工开物 · 学生号',
        akMasked: 'LTAI****9K2M',
        currency: 'CNY',
        ok: true,
        partial: false,
        balance: { available: '12.40', cash: '12.40', credit: '0.00', mybankCredit: '0.00', quotaLimit: null, currency: 'CNY' },
        coupons: {
          availableBalance: '158.50', availableCount: 1, totalCount: 2,
          earliestExpiry: new Date(now + 18 * day).toISOString(), earliestExpiryTs: now + 18 * day,
          expiringSoonCount: 1, expiringSoonBalance: '158.50', items: studentCoupons,
        },
        total: '170.90',
        errors: [],
        latencyMs: 412,
        fetchedAt,
      }],
      ['demo00000002', {
        id: 'demo00000002',
        name: '实验室公用账号',
        akMasked: 'LTAI****7Q4A',
        currency: 'CNY',
        ok: true,
        partial: false,
        balance: { available: '88.00', cash: '88.00', credit: '0.00', mybankCredit: null, quotaLimit: null, currency: 'CNY' },
        coupons: {
          availableBalance: '62.50', availableCount: 2, totalCount: 2,
          earliestExpiry: new Date(now + 46 * day).toISOString(), earliestExpiryTs: now + 46 * day,
          expiringSoonCount: 0, expiringSoonBalance: '0.00', items: labCoupons,
        },
        total: '150.50',
        errors: [],
        latencyMs: 368,
        fetchedAt,
      }],
      ['demo00000003', {
        id: 'demo00000003',
        name: '千问 API · 测试',
        akMasked: 'LTAI****3F8D',
        currency: 'CNY',
        ok: true,
        partial: true,
        balance: { available: '0.00', cash: '0.00', credit: '0.00', mybankCredit: null, quotaLimit: null, currency: 'CNY' },
        coupons: null,
        total: '0.00',
        errors: [{ scope: 'coupons', code: 'Forbidden.RAM', message: 'User not authorized to operate on the specified resource.' }],
        latencyMs: 501,
        fetchedAt,
      }],
      ['demo00000004', {
        id: 'demo00000004',
        name: '毕设备用号',
        akMasked: 'LTAI****5T1X',
        currency: 'CNY',
        ok: false,
        partial: false,
        balance: null,
        coupons: null,
        total: null,
        errors: [
          { scope: 'balance', code: 'InvalidAccessKeyId.NotFound', message: 'Specified access key is not found.' },
          { scope: 'coupons', code: 'InvalidAccessKeyId.NotFound', message: 'Specified access key is not found.' },
        ],
        latencyMs: 286,
        fetchedAt,
      }],
    ]);
    state.lastAt = now - 32000;
  }

  // ---------------------------------------------------------------- 事件绑定
  function bindEvents() {
    $('#btnAdd').addEventListener('click', () => openAccountDialog({ mode: 'add' }));
    $('#btnEmptyAdd').addEventListener('click', () => openAccountDialog({ mode: 'add' }));
    $('#btnEmptyBatch').addEventListener('click', () => openAccountDialog({ mode: 'add', tab: 'batch' }));
    $('#btnRefresh').addEventListener('click', () => queryAccounts());
    $('#btnSettings').addEventListener('click', openSettings);
    $('#btnTheme').addEventListener('click', cycleTheme);
    $('#btnSave').disabled = false;

    $('#accountForm').addEventListener('submit', onAccountSubmit);
    $('#btnTest').addEventListener('click', testConnection);

    $$('#accountTabs .tab').forEach((tab) => {
      tab.addEventListener('click', () => switchTab(tab.dataset.tab));
    });

    $('#toggleSk').addEventListener('click', () => {
      const input = $('#fSk');
      const show = input.type === 'password';
      input.type = show ? 'text' : 'password';
      $('#toggleSk use').setAttribute('href', show ? '#i-eye-off' : '#i-eye');
      $('#toggleSk').setAttribute('aria-label', show ? '隐藏 Secret' : '显示 Secret');
      input.focus();
    });

    $('#batchText').addEventListener('input', () => {
      clearTimeout(batchPreviewTimer);
      batchPreviewTimer = setTimeout(updateBatchPreview, 140);
    });

    $('#btnPickFile').addEventListener('click', () => $('#batchFile').click());
    $('#batchFile').addEventListener('change', async (event) => {
      const file = event.target.files && event.target.files[0];
      event.target.value = '';
      if (!file) return;
      if (file.size > 1024 * 1024) {
        toast('文件太大了（上限 1 MB）', 'err');
        return;
      }
      try {
        $('#batchText').value = await file.text();
        updateBatchPreview();
      } catch (error) {
        toast(`读取文件失败：${error.message}`, 'err');
      }
    });

    // 表格 / 移动端卡片：点击看明细，按钮做具体操作
    const onAccountClick = (event) => {
      const row = event.target.closest('[data-id]');
      if (!row || !row.dataset.id) return;
      const accountId = row.dataset.id;
      const actionButton = event.target.closest('button[data-act]');
      if (!actionButton) {
        openDetail(accountId);
        return;
      }
      event.stopPropagation();
      const action = actionButton.dataset.act;
      if (action === 'refresh') {
        if (state.demo) {
          toast('演示模式：示例数据保持不变', 'ok');
          return;
        }
        queryAccounts([accountId]);
      } else if (action === 'edit') {
        const account = state.accounts.find((a) => a.id === accountId);
        if (account) openAccountDialog({ mode: 'edit', account });
      } else if (action === 'delete') {
        deleteAccount(accountId);
      }
    };
    $('#accountRows').addEventListener('click', onAccountClick);
    $('#accountCards').addEventListener('click', onAccountClick);

    $('#searchInput').addEventListener('input', (event) => {
      state.filter = event.target.value;
      renderTable();
    });

    $('#exportMenu').addEventListener('click', (event) => {
      const button = event.target.closest('[data-export]');
      if (!button) return;
      event.preventDefault();
      $('#exportMenu').open = false;
      const kind = button.dataset.export;
      if (kind === 'csv') exportCsv();
      else if (kind === 'json') exportJson();
      else if (kind === 'copy') copySummary();
    });

    document.addEventListener('click', (event) => {
      const menu = $('#exportMenu');
      if (menu.open && !menu.contains(event.target)) menu.open = false;
    });

    // 弹窗通用关闭
    document.addEventListener('click', (event) => {
      const close = event.target.closest('[data-close]');
      if (close) {
        const dialog = close.closest('dialog');
        if (dialog) dialog.close();
      }
    });

    $$('dialog.modal').forEach((dialog) => {
      dialog.addEventListener('click', (event) => {
        if (event.target === dialog) dialog.close();
      });
    });

    $('#confirmOk').addEventListener('click', async () => {
      const handler = confirmHandler;
      confirmHandler = null;
      $('#confirmDialog').close();
      if (handler) {
        try {
          await handler();
        } catch (error) {
          toast(error.message, 'err');
        }
      }
    });

    $('#detailDialog').addEventListener('close', () => { delete $('#detailDialog').dataset.id; });

    $('#btnDetailRefresh').addEventListener('click', () => {
      const accountId = $('#detailDialog').dataset.id;
      if (accountId) {
        if (state.demo) toast('演示模式：示例数据保持不变', 'ok');
        else queryAccounts([accountId]);
      }
    });

    $('#btnDetailEdit').addEventListener('click', () => {
      const accountId = $('#detailDialog').dataset.id;
      const account = state.accounts.find((a) => a.id === accountId);
      if (account) {
        $('#detailDialog').close();
        openAccountDialog({ mode: 'edit', account });
      }
    });

    $('#setAuto').addEventListener('change', (event) => {
      state.settings.autoMin = Number(event.target.value) || 0;
      savePrefs();
      setupAutoRefresh();
      toast(state.settings.autoMin ? `自动刷新：每 ${state.settings.autoMin} 分钟` : '自动刷新已关闭', 'ok');
    });

    $('#setWidget').addEventListener('change', (event) => {
      state.settings.widget = event.target.checked;
      savePrefs();
      renderWidget();
    });

    $('#setTheme').addEventListener('change', (event) => {
      state.theme = event.target.value;
      savePrefs();
      applyTheme();
    });

    $('#btnClearAll').addEventListener('click', clearAll);
    $('#btnSettingsClose')?.addEventListener('click', () => $('#settingsDialog').close());

    document.addEventListener('visibilitychange', () => {
      if (document.hidden) return;
      const minutes = Number(state.settings.autoMin) || 0;
      if (minutes > 0 && state.lastAt && Date.now() - state.lastAt > minutes * 60000) {
        queryAccounts(null, { silent: true });
      }
    });

    const darkQuery = window.matchMedia('(prefers-color-scheme: dark)');
    if (darkQuery.addEventListener) darkQuery.addEventListener('change', applyTheme);
  }

  // ---------------------------------------------------------------- 启动
  async function loadHealth() {
    try {
      const health = await api('/api/health');
      state.protection = health.protection || 'dpapi';
      if (state.protection !== 'dpapi') {
        const notice = $('#protectionNotice');
        notice.hidden = false;
        $('#protectionText').textContent = state.protection === 'mixed'
          ? '部分 Secret 未使用 Windows DPAPI 加密保存。建议在本机重新保存这些账户的 SK。'
          : '当前环境无法使用 Windows DPAPI，Secret 以 base64 混淆保存。请勿在共享电脑上保存重要凭据。';
      }
    } catch (error) {
      toast(error.message, 'err');
    }
  }

  async function boot() {
    loadPrefs();
    applyTheme();
    initWidget();
    bindEvents();

    if (state.demo) {
      $('#demoNotice').hidden = false;
      seedDemo();
      renderAll();
      setupAutoRefresh();
      return;
    }

    renderAll();
    await loadHealth();
    try {
      await reloadAccounts();
    } catch (error) {
      toast(error.message, 'err');
    }
    renderAll();
    setupAutoRefresh();

    if (state.accounts.some((a) => a.enabled)) {
      queryAccounts(null, { silent: true });
    }
  }

  boot();
})();
