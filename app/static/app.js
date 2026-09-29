'use strict';
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const finite = x => typeof x === 'number' && Number.isFinite(x);
const number = (x, c = 'KRW') => finite(x) ? new Intl.NumberFormat('ko-KR', {style:'currency',currency:c,maximumFractionDigits:c==='KRW'?0:2}).format(x) : '—';
const stamp = t => finite(t) && t > 0 ? new Date(t * 1000) : null;
const clock = t => stamp(t)?.toLocaleTimeString('ko-KR',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}) || '—';
const dateTime = t => stamp(t)?.toLocaleString('ko-KR',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}) || '기록 없음';
const percent = x => finite(x) ? (x >= 0 ? '+' : '') + x.toFixed(2) + '%' : '—';
const names = {selector:'종목 선정',planner:'플래너',fundamental:'기업 분석가',technical:'차트 분석가',news:'뉴스 분석가',critic:'반대 검토자',director:'디렉터'};
const exitNames = {stop_loss:'손절 기준 도달',take_profit:'익절 기준 도달',time_stop:'보유 기한 도달',time_exit:'보유 기한 도달',max_holding:'보유 기한 도달',expiry:'보유 기한 도달',daily_loss_limit:'일중 손실 한도',director:'디렉터 매도 의견',liquidation:'전량 모의매도'};
const safeUrl = value => { try { const url = new URL(value); return ['https:','http:'].includes(url.protocol) ? url.href : null; } catch { return null; } };
const roleList = s => Array.isArray(s.config.roles) && s.config.roles.length ? s.config.roles : Object.entries(names).filter(([id]) => s.strategy_mode === 'intraday' || id !== 'planner').map(([id,name]) => ({id,name}));
const plainPercent = value => finite(value) ? value.toLocaleString('ko-KR',{maximumFractionDigits:2}) + '%' : '—';
const quantity = value => finite(value) ? Math.floor(value).toLocaleString('ko-KR') + '주' : '—';
let state = null, loggedIn = false, reportKey = '', busy = false, timer = null, loading = false, executionDirty = false, experimentId = null, evalHorizon = '60';
const ruleNames = {golden_cross:'골든크로스', momentum:'모멘텀', mean_reversion:'평균회귀', breakout:'돌파'};
const roleIcons = {selector:'target', planner:'clipboard', fundamental:'building', technical:'chart', news:'news', critic:'scale', director:'star'};
const icon = (name, cls = '') => `<svg class="ic ${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const avatarClass = symbol => 'av' + ([...String(symbol)].reduce((sum, ch) => sum + ch.charCodeAt(0), 0) % 6);
// The workspace re-renders every 2s. Replacing identical markup would restart animations, drop hover state and
// swallow clicks, so containers are only rewritten when their HTML actually changes.
function setHtml(id, html) {
  const el = $(id);
  if (el._html === html) return;
  el.innerHTML = html;
  el._html = html;
  el.querySelectorAll('[data-w]').forEach(n => n.style.setProperty('--w', n.dataset.w));
  el.querySelectorAll('[data-pct]').forEach(n => n.style.setProperty('--pct', n.dataset.pct));
}
function setRing(id, pct, label) {
  $(id).style.setProperty('--pct', String(Math.max(0, Math.min(100, pct))));
  $(id + '-pct').textContent = label;
  $(id).setAttribute('aria-label', '오늘 AI 호출 사용량 ' + label);
}
function smoothPath(points, low = -Infinity, high = Infinity) {
  if (points.length < 2) return '';
  const clamp = (v, a, b) => Math.max(Math.min(a, b), Math.min(Math.max(a, b), v));
  let d = `M${points[0][0].toFixed(1)},${points[0][1].toFixed(1)}`;
  for (let i = 0; i < points.length - 1; i++) {
    const p0 = points[i - 1] || points[i], p1 = points[i], p2 = points[i + 1], p3 = points[i + 2] || p2, t = 1 / 6;
    // Control points stay between the neighbouring values so the curve never overshoots a real reading.
    const y1 = clamp(p1[1] + (p2[1] - p0[1]) * t, p1[1], p2[1]), y2 = clamp(p2[1] - (p3[1] - p1[1]) * t, p1[1], p2[1]);
    d += ` C${(p1[0] + (p2[0] - p0[0]) * t).toFixed(1)},${clamp(y1, low, high).toFixed(1)} ${(p2[0] - (p3[0] - p1[0]) * t).toFixed(1)},${clamp(y2, low, high).toFixed(1)} ${p2[0].toFixed(1)},${p2[1].toFixed(1)}`;
  }
  return d;
}
const themeMeta = document.querySelector('meta[name="theme-color"]');
function syncTheme() {
  const dark = document.documentElement.dataset.theme === 'dark';
  $('theme-toggle').setAttribute('aria-pressed', String(dark));
  $('theme-toggle').title = dark ? '라이트 테마로 전환' : '다크 테마로 전환';
  if (themeMeta) themeMeta.setAttribute('content', dark ? '#06071a' : '#e8ecfb');
}
$('theme-toggle').onclick = () => {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('stocklab-theme', next); } catch (e) { /* private mode: the choice just isn't remembered */ }
  syncTheme();
};
syncTheme();
const navLinks = [...document.querySelectorAll('.topnav a')];
const navSections = navLinks.map(a => document.querySelector(a.getAttribute('href'))).filter(Boolean);
let navCurrent = '';
function updateNav() {
  const line = window.innerHeight * .32;
  let current = navSections[0];
  for (const section of navSections) if (section.getBoundingClientRect().top <= line) current = section;
  navLinks.forEach(a => a.setAttribute('aria-current', String(!!current && a.getAttribute('href') === '#' + current.id)));
  if (current && current.id !== navCurrent) {
    navCurrent = current.id;
    // On narrow screens the tab strip scrolls sideways: keep the active tab in view.
    const strip = document.querySelector('.topnav'), active = navLinks.find(a => a.getAttribute('aria-current') === 'true');
    if (strip && active && strip.scrollWidth > strip.clientWidth) strip.scrollTo({left: active.offsetLeft - (strip.clientWidth - active.clientWidth) / 2, behavior: 'smooth'});
  }
}
let navTick = false;
addEventListener('scroll', () => { if (!navTick) { navTick = true; requestAnimationFrame(() => { navTick = false; updateNav(); }); } }, {passive: true});
addEventListener('resize', updateNav);

function toast(message) {
  $('toast').textContent = message;
  $('toast').hidden = false;
  clearTimeout(timer);
  timer = setTimeout(() => $('toast').hidden = true, 6000);
}
async function api(path, body) {
  const res = await fetch('/api/' + path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? {} : {'Content-Type':'application/json','X-Stocklab-Action':'1'},
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  const data = await res.json();
  if (!res.ok) {
    if (res.status === 401 && path !== 'login') showLogin();
    throw Error(typeof data.detail === 'string' ? data.detail : '입력값과 연결 상태를 확인해 주세요.');
  }
  return data;
}
function showLogin() { loggedIn = false; $('login-view').hidden = false; $('workspace').hidden = true; }
function showWorkspace() { loggedIn = true; $('login-view').hidden = true; $('workspace').hidden = false; }
async function reload() {
  if (loading) return;
  loading = true;
  try {
    state = await api('state');
    showWorkspace();
    render();
  } catch (e) {
    if (loggedIn) {
      $('error-banner').textContent = '서버와 연결되지 않았습니다. 표시된 시세와 평가손익은 최신이 아닐 수 있습니다.';
      $('error-banner').hidden = false;
    }
  } finally { loading = false; }
}
async function act(path, body = {}, message) {
  if (busy) return false;
  busy = true;
  try {
    await api(path, body);
    if (message) toast(message);
    await reload();
    return true;
  } catch (e) { toast(e.message); return false; }
  finally { busy = false; }
}
$('login-form').addEventListener('submit', async e => {
  e.preventDefault();
  try {
    await api('login',{password:$('password').value});
    $('password').value = '';
    $('login-error').textContent = '';
    await reload();
  } catch (e) { $('login-error').textContent = e.message; }
});
$('logout').onclick = async () => { await act('logout'); showLogin(); };
$('start').onclick = () => act('start', {}, state?.execution_mode === 'auto' ? '자동 모의매매를 시작했습니다. 조건을 통과한 제안은 개별 승인 없이 체결됩니다.' : '분석을 시작했습니다. 매매 제안은 직접 승인 후 모의체결됩니다.');
$('stop').onclick = () => act('stop', {}, state?.strategy_mode === 'intraday' ? '중지했습니다. 보유 종목은 유지되며 손절·익절·보유 기한 감시도 멈춥니다.' : '중지했습니다. 보유 종목은 유지됩니다.');
$('liquidate').onclick = () => { $('liquidation-dialog').returnValue = ''; $('liquidation-dialog').showModal(); };
$('liquidation-dialog').addEventListener('close', () => {
  if ($('liquidation-dialog').returnValue === 'confirm') act('liquidate',{confirmation:'전량 모의매도'},'전량 모의매도를 요청했습니다.');
});
$('analyze').onclick = () => act('analyze',{symbol:$('analyze-symbol').value},'분석을 요청했습니다.');
$('chart-currency').onchange = () => drawChart();
$('proposal-list').onclick = e => {
  const b = e.target.closest('button[data-id]');
  if (b) act('proposals/' + encodeURIComponent(b.dataset.id) + '/' + b.dataset.action, {}, b.dataset.action === 'approve' ? '승인한 제안의 모의체결을 처리했습니다.' : '제안을 거절했습니다.');
};
$('execution-mode').onchange = () => { executionDirty = true; updateExecutionControls(); };
$('execution-apply').onclick = async () => {
  if ($('execution-mode').value === 'auto') {
    $('execution-dialog').returnValue = '';
    $('execution-dialog').showModal();
  } else {
    if (await act('settings/execution',{mode:'manual'},'직접 승인 방식으로 변경했습니다. 시작 버튼을 눌러 운용하세요.')) executionDirty = false;
  }
};
$('execution-dialog').addEventListener('close', async () => {
  if ($('execution-dialog').returnValue === 'confirm') {
    if (await act('settings/execution',{mode:'auto'},'자동 모의매매를 설정했습니다. 시작 버튼을 누르면 운용합니다.')) executionDirty = false;
  }
});
$('experiment-open').onclick = () => {
  $('experiment-error').textContent = '';
  $('experiment-input-name').value = '모의투자 ' + new Date().toLocaleDateString('ko-KR');
  $('seed-krw').value = '1000000';
  $('seed-usd').value = '1000';
  $('max-order-pct').value = '30';
  $('strategy-mode').value = 'intraday';
  $('include-leveraged-etfs').checked = true;
  $('risk-per-trade').value = '0.5';
  $('daily-loss-limit').value = '2';
  $('max-holding-minutes').value = '120';
  updateStrategyForm();
  $('experiment-dialog').showModal();
};
$('strategy-mode').onchange = updateStrategyForm;
function updateStrategyForm() {
  const intraday = $('strategy-mode').value === 'intraday';
  $('intraday-settings').hidden = !intraday;
  $('intraday-settings').disabled = !intraday;
  $('strategy-form-description').textContent = intraday ? '플래너 → 기업·차트·뉴스 병렬 조사 → 반대 검토 → 디렉터 순서로 분석하고, 목표 비중과 손절 위험을 바탕으로 수량을 정합니다.' : '기존 5역할 분석 방식입니다. 장중 전략의 손절·익절·보유 기한 관리를 추가하지 않습니다.';
}
$('experiment-cancel').onclick = () => $('experiment-dialog').close();
$('experiment-form').addEventListener('submit', async e => {
  e.preventDefault();
  if (busy) return;
  const name = $('experiment-input-name').value.trim();
  const seed_krw = Number($('seed-krw').value), seed_usd = Number($('seed-usd').value), max_order_pct = Number($('max-order-pct').value);
  const strategy_mode = $('strategy-mode').value;
  const settings = {include_leveraged_etfs:$('include-leveraged-etfs').checked,risk_per_trade_pct:Number($('risk-per-trade').value),daily_loss_limit_pct:Number($('daily-loss-limit').value),max_holding_minutes:Number($('max-holding-minutes').value)};
  if (!name || ![seed_krw,seed_usd,max_order_pct].every(Number.isFinite) || seed_krw < 0 || seed_usd < 0 || seed_krw + seed_usd <= 0 || max_order_pct < 1 || max_order_pct > 30) {
    $('experiment-error').textContent = '실험 이름과 원금, 매수 한도를 확인해 주세요. 최소 한 통화의 원금은 0보다 커야 합니다.';
    return;
  }
  if (strategy_mode === 'intraday' && (!Object.values(settings).every(v => typeof v === 'boolean' || finite(v)) || settings.risk_per_trade_pct < .1 || settings.risk_per_trade_pct > 2 || settings.daily_loss_limit_pct < 1 || settings.daily_loss_limit_pct > 10 || settings.max_holding_minutes < 15 || settings.max_holding_minutes > 240 || !Number.isInteger(settings.max_holding_minutes))) {
    $('experiment-error').textContent = '손절 위험은 0.1~2%, 일중 손실 한도는 1~10%, 보유 시간은 15~240분 범위로 입력해 주세요.';
    return;
  }
  busy = true;
  $('experiment-create').disabled = true;
  $('experiment-error').textContent = '';
  try {
    await api('experiments',{name,seed_krw,seed_usd,max_order_pct,strategy_mode,...settings,confirmation:'새 실험 시작'});
    executionDirty = false;
    $('experiment-dialog').close();
    toast('이전 실험을 보관하고 새 실험을 만들었습니다. 운용 방식을 선택한 뒤 시작하세요.');
    await reload();
  } catch (err) { $('experiment-error').textContent = err.message; }
  finally { busy = false; $('experiment-create').disabled = false; }
});
$('history-open').onclick = async () => {
  $('history-dialog').showModal();
  $('experiment-history').innerHTML = '<p class="muted small">기록을 불러오는 중입니다.</p>';
  try {
    const data = await api('experiments');
    $('experiment-history').innerHTML = data.experiments.length ? data.experiments.map(e => {
      const pnl = e.performance || {}, initial = e.initial || {KRW:pnl.KRW?.initial,USD:pnl.USD?.initial};
      const returns = ['KRW','USD'].filter(c => finite(pnl[c]?.return_pct)).map(c => c + ' ' + percent(pnl[c].return_pct)).join(' · ');
      const stale = ['KRW','USD'].some(c => pnl[c]?.valuation_fresh === false);
      return `<article class="archive-item"><div><h3>${esc(e.name || '지난 실험')}</h3><p class="muted small">종료 ${dateTime(e.ended_at || e.created)} · ${Number(e.trades_count || 0)}건 체결${finite(e.positions_count) ? ' · 보유 ' + e.positions_count + '종목' : ''}</p><p class="small">원금 ${number(initial.KRW,'KRW')} · ${number(initial.USD,'USD')}</p>${returns ? '<p class="small">종료 수익률 ' + esc(returns) + '</p>' : ''}${stale ? '<p class="small stale">종료 당시 최신 시세가 아닌 평가가 포함됩니다.</p>' : ''}</div><a class="archive-download" href="/api/experiments/${encodeURIComponent(e.id)}/export" download>기록 받기 ↗</a></article>`;
    }).join('') : empty('보관된 실험이 없습니다','새 실험을 만들면 이전 실험의 기록이 여기에 보관됩니다.');
  } catch (err) { $('experiment-history').innerHTML = '<p class="form-error">' + esc(err.message) + '</p>'; }
};
function empty(title, desc, symbol = 'spark') {
  return `<div class="empty"><span class="empty-icon" aria-hidden="true">${icon(symbol)}</span><strong>${esc(title)}</strong><span>${esc(desc)}</span></div>`;
}
function updateExecutionControls() {
  if (!state) return;
  const locked = state.running || state.liquidating;
  $('execution-mode').disabled = locked;
  $('execution-apply').disabled = locked || $('execution-mode').value === (state.execution_mode || 'manual');
  $('experiment-open').disabled = locked;
}
function renderPerformance(s, c, id) {
  const p = s.performance?.[c] || {};
  const initial = finite(p.initial) ? p.initial : s.initial[c];
  const equity = finite(p.equity) ? p.equity : s.equity[c];
  const profit = finite(p.profit) ? p.profit : equity - initial;
  const pct = 'return_pct' in p ? p.return_pct : initial > 0 ? profit / initial * 100 : null;
  $('nav-' + id).textContent = number(equity,c);
  $('cash-' + id).textContent = '현금 ' + number(s.cash[c],c);
  $('return-' + id).textContent = pct === null ? '원금 0 · 수익률 없음' : percent(pct);
  $('return-' + id).className = finite(pct) ? (pct >= 0 ? 'gain' : 'loss') : 'muted';
  const rows = [
    ['가상 원금',number(initial,c),''],
    ['총 평가손익',number(profit,c),profit >= 0 ? 'gain' : 'loss'],
    ['실현 / 미실현',number(p.realized,c) + ' / ' + number(p.unrealized,c),''],
    ['누적 매매비용',number(p.costs,c),''],
    ['최대 낙폭',finite(p.max_drawdown_pct) ? p.max_drawdown_pct.toFixed(2) + '%' : '—','']
  ];
  setHtml('performance-' + id, rows.map(([title,value,cls]) => `<div><dt>${title}</dt><dd class="${cls}">${value}</dd></div>`).join(''));
  const note = [];
  if (p.valuation_fresh === false) note.push('시세 확인 필요 · 마지막 가격 기준');
  else if (p.last_valuation_at) note.push('평가 ' + clock(p.last_valuation_at));
  if (p.drawdown_since) note.push('낙폭 기록 시작 ' + dateTime(p.drawdown_since));
  $('valuation-' + id).textContent = note.join(' · ');
  $('valuation-' + id).className = 'valuation small ' + (p.valuation_fresh === false ? 'stale' : 'muted');
}
function renderStrategy(s) {
  const intraday = s.strategy_mode === 'intraday', settings = s.strategy_settings || {}, risk = s.risk_status;
  $('strategy-label').textContent = intraday ? '전문가팀 · 장중 매매' : '기본 분석 · 기존 방식';
  $('strategy-description').textContent = intraday ? `손절 위험 ${plainPercent(settings.risk_per_trade_pct)} · 일중 손실 한도 ${plainPercent(settings.daily_loss_limit_pct)} · 최대 보유 ${finite(settings.max_holding_minutes) ? settings.max_holding_minutes + '분' : '—'} · ${settings.include_leveraged_etfs ? '레버리지·인버스 ETF 포함' : '레버리지 ETF 제외'}` : '현재 실험의 기존 분석 방식을 유지합니다. 전문가팀 장중 전략은 새 실험에서 선택할 수 있습니다.';
  $('risk-status').hidden = !intraday || !risk;
  if (intraday && risk) {
    const currencies = Array.isArray(risk.halted_currencies) ? risk.halted_currencies : [];
    const values = ['KRW','USD'].filter(c => finite(risk.day_pnl_pct?.[c])).map(c => `<span>${c} <b class="${risk.day_pnl_pct[c] >= 0 ? 'gain' : 'loss'}">${percent(risk.day_pnl_pct[c])}</b></span>`).join('');
    $('risk-status').className = 'risk-status' + (risk.halted ? ' halted' : '');
    setHtml('risk-status', `<div class="risk-day"><span>일중 손익률</span>${values || '<span class="muted">기준 수집 중</span>'}</div>${risk.halted ? `<p>${currencies.length ? esc(currencies.join(' · ')) + ' ' : ''}신규 매수 중단${risk.reason ? ' · ' + esc(risk.reason) : ''}. 실행 중인 청산 감시는 유지합니다.</p>` : ''}`);
  }
  $('monitor-warning').hidden = !intraday;
  if (intraday) {
    $('monitor-warning').className = 'monitor-warning small ' + (s.running ? '' : 'stale');
    $('monitor-warning').textContent = s.running ? (s.execution_mode === 'auto' ? '손절·익절·보유 기한을 감시하며 조건 충족 시 자동 모의매도를 시도합니다.' : '손절·익절·보유 기한을 감시하며 조건 충족 시 매도 제안을 만듭니다. 직접 승인해야 체결됩니다.') + ' AI 호출 한도에 도달해도 실행 중인 청산 감시는 계속됩니다.' : '현재 손절·익절·보유 기한 감시가 멈춰 있습니다. 중지는 보유 종목을 유지하며, 시작해야 감시가 재개됩니다.';
  }
}
function render() {
  const s = state, now = s.server_time, sim = s.mode === 'demo', auto = s.execution_mode === 'auto';
  $('clock').textContent = clock(now);
  $('run-status').textContent = s.liquidating ? '전량 매도 대기' : s.running ? (auto ? '자동 모의매매 중' : '분석 실행 중') : '중지됨';
  $('run-status').classList.toggle('running',s.running);
  $('start').disabled = s.running || s.liquidating;
  $('stop').disabled = !s.running && !s.liquidating;
  $('liquidate').disabled = !Object.keys(s.positions).length || s.liquidating;
  $('analyze').disabled = !s.running || s.runs.some(r => r.status === 'running');
  $('mode-description').textContent = sim ? '시험 모드 · 합성 시세와 고정 응답으로 동작 확인' : `토스증권 시세 · ${(s.config.providers || ['gemini']).map(x => ({claude:'Claude',codex:'Codex',gemini:'Gemini'})[x] || x).join(' → ')} 분석 · 가상 자금`;
  $('mode-banner').textContent = sim ? '시험 모드입니다. 아래 가격과 수익률은 실제 시장 성과가 아니며, 에이전트도 고정 응답입니다. 실제 시세 연결에는 MARKET_MODE=toss를 사용하세요.' : `실제 시세를 ${s.config.poll}초마다 조회하고 AI는 ${Math.round(s.config.interval/60)}분 간격으로 분석합니다. 정규장·최신 호가에서만 가상 체결하며 실제 주문은 전송하지 않습니다.`;
  $('error-banner').hidden = !s.last_error;
  $('error-banner').textContent = s.last_error;
  if (experimentId !== s.experiment_id) {
    experimentId = s.experiment_id;
    executionDirty = false;
  }
  if (!executionDirty || s.running || s.liquidating) $('execution-mode').value = auto ? 'auto' : 'manual';
  updateExecutionControls();
  $('experiment-name').textContent = s.experiment_name || '기존 모의투자 기록';
  $('experiment-started').textContent = s.started_at ? '시작 ' + dateTime(s.started_at) : '';
  const ratio = finite(s.max_order_ratio) ? Math.round(s.max_order_ratio * 100) : 10;
  $('execution-description').textContent = (auto ? '자동 모의매매 · 개별 승인 없이 조건 충족 시 체결' : '직접 승인 · 매매 제안을 검토한 뒤 체결') + ` · 1회 매수 ${ratio}% / 종목당 30% 한도`;
  renderStrategy(s);
  $('scheduler-status').textContent = s.scheduler_status || (s.running ? '관심종목을 순서대로 분석합니다.' : '운용 방식과 가상 원금을 확인한 뒤 시작하세요. 설정은 중지 상태에서 변경할 수 있습니다.');
  renderPerformance(s,'KRW','kr');
  renderPerformance(s,'USD','us');
  const date = stamp(now)?.toISOString().slice(0,10);
  $('ai-usage').textContent = sim ? '외부 호출 없음' : `${s.daily_ai[date] || 0} / ${s.config.daily_limit}`;
  const calls = Math.max(1, s.config.analysis_calls_per_cycle || roleList(s).length), dailyLimit = s.config.daily_limit || 0, used = s.daily_ai[date] || 0;
  const usedPct = dailyLimit ? Math.min(100, used / dailyLimit * 100) : 0;
  setRing('ai-ring', sim ? 0 : usedPct, sim ? '—' : Math.round(usedPct) + '%');
  $('ai-cycle-budget').textContent = sim ? `${calls}개 역할의 고정 응답으로 흐름을 확인합니다.` : `분석 1회 ${calls}호출 · 일 ${dailyLimit}호출로 완전 분석 최대 ${Math.floor(dailyLimit/calls)}회 · 남은 호출로 최대 ${Math.floor(Math.max(0,dailyLimit-used)/calls)}회`;
  $('trade-count').textContent = `${s.trades_count}건 모의체결`;
  $('next-run').textContent = s.running ? `분석 간격 ${Math.round(s.config.interval/60)}분` : '현재 중지 상태';
  $('poll-label').textContent = `${s.config.poll}초 조회`;
  setHtml('watchlist', s.instruments.map(i => {
    const q = s.quotes[i.symbol], stale = !q || !finite(q.asof) || now - q.asof > 30 || q.asof - now > 5;
    const leverage = finite(i.leverage_factor) && Math.abs(i.leverage_factor) > 1 ? `<span class="etf-tag">ETF ${i.leverage_factor > 0 ? '+' : ''}${i.leverage_factor}배</span>` : '';
    return `<div class="watchrow"><span class="avatar ${avatarClass(i.symbol)}" aria-hidden="true">${esc([...String(i.name)][0] || '?')}</span><div><span class="name">${esc(i.name)}</span>${leverage}<span class="ticker">${esc(i.symbol)} · ${esc(i.market)}</span><div class="session">${esc(q?.session || '시세 대기')}${stale ? ' · 시세 확인 필요' : ''}</div></div><div><div class="price">${q ? number(q.last,i.currency) : '—'}</div><div class="asof">${q?.asof ? clock(q.asof) + ' 기준' : '기준 시각 없음'}</div></div></div>`;
  }).join(''));
  if ([...$('analyze-symbol').options].map(o => o.value).join('|') !== s.instruments.map(i => i.symbol).join('|')) {
    const selected = $('analyze-symbol').value;
    $('analyze-symbol').innerHTML = s.instruments.map(i => `<option value="${esc(i.symbol)}">${esc(i.name)}</option>`).join('');
    if (s.instruments.some(i => i.symbol === selected)) $('analyze-symbol').value = selected;
  }
  renderDecisions(s,now,auto);
  renderTeam(s);
  renderEvaluation(s);
  renderLive(s);
  renderIntel(s);
  setHtml('positions', Object.entries(s.positions).map(([symbol,p]) => {
    const i = s.instruments.find(i => i.symbol === symbol), q = s.quotes[symbol], pnl = q ? q.last * p.quantity - (p.cost_basis ?? p.average * p.quantity) : null;
    const currency = i?.currency || p.currency;
    const thesis = p.entry_thesis ? `<div class="entry-thesis" title="${esc(p.entry_thesis)}">${esc(p.entry_thesis)}</div>` : '';
    return `<tr><td>${esc(i?.name || symbol)}${thesis}</td><td>${p.quantity}주</td><td>${number(p.average,currency)}</td><td class="${pnl === null ? 'muted' : pnl >= 0 ? 'gain' : 'loss'}">${pnl === null ? '시세 대기' : number(pnl,currency)}</td><td>${number(p.stop_price,currency)}</td><td>${number(p.take_profit_price,currency)}</td><td>${p.expires_at ? dateTime(p.expires_at) + (!s.running ? '<span class="monitor-off">감시 중지</span>' : p.expires_at <= now ? '<span class="monitor-off">기한 도달 · 청산 대기</span>' : '') : '—'}</td></tr>`;
  }).join('') || '<tr><td colspan="7" class="empty-cell">아직 보유한 종목이 없습니다.</td></tr>');
  setHtml('events', s.events.slice(-15).reverse().map(e => `<div class="event ${e.level === 'warning' ? 'warning' : ''}"><time>${clock(e.time)}</time><p>${esc(e.message)}</p></div>`).join(''));
  setHtml('trades', s.trades.slice().reverse().map(t => `<tr><td>${dateTime(t.time)}</td><td>${esc(s.instruments.find(i => i.symbol === t.symbol)?.name || t.symbol)}</td><td>${t.side === 'BUY' ? '매수' : '매도'}</td><td>${t.liquidation || t.execution_mode === 'liquidation' ? '전량매도' : t.execution_mode === 'auto' ? '자동' : '직접 승인'}</td><td>${t.quantity}주</td><td>${number(t.price,t.currency)}</td><td>${number(t.fee,t.currency)}</td><td class="${t.realized >= 0 ? 'gain' : 'loss'}">${t.side === 'SELL' ? number(t.realized,t.currency) : '—'}</td><td>${esc(t.exit_reason ? exitNames[t.exit_reason] || t.exit_reason : '—')}</td></tr>`).join('') || '<tr><td colspan="9" class="empty-cell">모의매매가 체결되면 이곳에 기록됩니다.</td></tr>');
  $('cost-note').textContent = `시뮬레이션 비용 가정: 매매비용 국내 ${s.config.fee_kr_bps}bp / 미국 ${s.config.fee_us_bps}bp, 국내 매도세금 ${s.config.sell_tax_kr_bps}bp, 슬리피지 ${s.config.slippage_bps}bp. 실제 수수료·세금과 다를 수 있습니다. 1bp = 0.01%. 자동 환전은 하지 않습니다.`;
  drawChart();
  drawSpark('spark-kr', 'KRW');
  drawSpark('spark-us', 'USD');
  updateNav();
}
function renderDecisions(s,now,auto) {
  const pending = s.proposals.filter(p => p.status === 'pending' && p.expires > now);
  $('decision-title').textContent = auto ? '자동 운용 현황' : '나의 결정';
  $('pending-count').textContent = auto ? 'AUTO' : pending.length;
  if (auto) {
    const recent = s.trades.filter(t => t.execution_mode === 'auto').slice(-4).reverse();
    setHtml('proposal-list', `<div class="auto-note"><span class="pill">자동 모의매매</span><h3>${s.running ? '조건을 통과한 제안을 자동 체결합니다' : '시작을 기다리고 있습니다'}</h3><p>개별 승인 없이 가상 계좌에서만 매매합니다. 관망 의견이거나 정규장·시세·호가·자금 한도를 충족하지 못하면 거래하지 않습니다.</p>${s.strategy_mode === 'intraday' ? '<p>목표 비중·손절 위험·현금·매수 한도를 함께 반영해 종목마다 수량을 정합니다. 손절·익절·보유 기한 감시는 실행 중에만 작동합니다.</p>' : ''}<p class="muted small">1주 가격이 매수 한도를 넘는 종목도 건너뜁니다. 분석·체결이 없을 때는 운영 기록을 확인하세요.</p></div>` + (recent.length ? '<div class="auto-trades">' + recent.map(t => `<div><span>${esc(s.instruments.find(i => i.symbol === t.symbol)?.name || t.symbol)} <b>${t.side === 'BUY' ? '매수' : '매도'} ${t.quantity}주</b>${t.exit_reason ? '<small>' + esc(exitNames[t.exit_reason] || t.exit_reason) + '</small>' : ''}</span><span class="muted small">${clock(t.time)}</span></div>`).join('') + '</div>' : empty('자동 체결 기록이 없습니다','시장 상황과 분석 결과에 따라 매매 없이 대기할 수 있습니다.')));
    return;
  }
  setHtml('proposal-list', pending.length ? pending.map(p => {
    const i = s.instruments.find(i => i.symbol === p.symbol), remaining = Math.max(0,Math.floor(p.expires-now));
    return `<article class="proposal"><div class="proposal-head"><h3>${esc(i?.name || p.symbol)}</h3><span class="side ${p.side === 'SELL' ? 'sell' : ''}">${p.side === 'BUY' ? '매수 제안' : '매도 제안'}</span></div><div class="proposal-values"><div><small>수량</small><strong>${p.quantity}주</strong></div><div><small>기준 호가</small><strong>${number(p.reference_price,i?.currency)}</strong></div><div><small>예상 금액 · 비용 제외</small><strong>${number(p.reference_price*p.quantity,i?.currency)}</strong></div></div>${tradePlan(p)}${p.exit_reason ? '<div class="exit-trigger">청산 사유 · ' + esc(exitNames[p.exit_reason] || p.exit_reason) + '</div>' : ''}<p>${esc(p.summary)}</p>${p.sizing ? sizingDetails(p.sizing,i?.currency) : ''}<div class="risks">${(p.risks || []).map(esc).join(' · ')}</div><div class="proposal-actions"><button class="secondary" data-id="${esc(p.id)}" data-action="reject">거절</button><button class="primary" data-id="${esc(p.id)}" data-action="approve">승인하고 모의체결</button></div><p class="expires">${remaining}초 후 만료 · 가격 0.5% 이상 변동 시 재분석</p></article>`;
  }).join('') : empty('결정할 제안이 없습니다',s.running ? '분석이 끝나면 매매안과 반대 의견을 확인할 수 있습니다.' : '시작을 누르면 투자팀이 관심종목을 순서대로 분석합니다.'));
}
function tradePlan(report) {
  const values = [['목표 비중',report.target_weight_pct,plainPercent],['손절 간격',report.stop_loss_pct,plainPercent],['익절 간격',report.take_profit_pct,plainPercent],['보유 상한',report.max_holding_minutes,v => v + '분']].filter(([,value]) => finite(value) && value > 0);
  if (!values.length) return '';
  return '<dl class="trade-plan">' + values.map(([label,value,format]) => `<div><dt>${label}</dt><dd>${esc(format(value))}</dd></div>`).join('') + '</dl>';
}
function sizingDetails(sizing,currency) {
  const items = [['목표 비중 기준',sizing.target_quantity],['손절 위험 기준',sizing.risk_quantity],['매수 가능 한도',sizing.max_buy_quantity]].filter(([,value]) => finite(value));
  return `<div class="sizing-details"><div class="sizing-limits">${items.map(([label,value]) => `<span>${label} <b>${quantity(value)}</b></span>`).join('')}${finite(sizing.estimated_stop_risk) ? `<span>예상 손절 위험 <b>${number(sizing.estimated_stop_risk,currency)}</b></span>` : ''}</div>${sizing.reason ? '<p class="muted small">' + esc(sizing.reason) + '</p>' : ''}</div>`;
}
function reportExtras(report) {
  let html = '';
  if (report.role === 'selector' && Array.isArray(report.inputs) && report.inputs.length) {
    const fmt = (x, d = 2, suffix = '') => finite(x) ? x.toFixed(d) + suffix : '—';
    const shares = x => finite(x) ? (x > 0 ? '+' : '') + x.toLocaleString() : '—';
    html += '<div class="briefing"><h4>AI가 본 후보 지표</h4><div class="table-wrap"><table class="eval-table"><thead><tr><th>종목</th><th>20분</th><th>변동성</th><th>거래량비</th><th>스프레드</th><th>거래량 순위</th><th>외국인 순매수</th><th>기관 순매수</th></tr></thead><tbody>'
      + report.inputs.map(i => `<tr><td>${esc(i.symbol)}${i.symbol === report.symbol ? ' ✓' : ''}</td><td>${fmt(i.return_20m_pct, 2, '%')}</td><td>${fmt(i.volatility_1m_pct, 3, '%')}</td><td>${fmt(i.volume_ratio_5m, 2, '배')}</td><td>${fmt(i.spread_bps, 1, 'bp')}</td><td>${esc(i.rankings ? i.rankings.volume_rank : '—')}</td><td>${shares(i.investor_flows?.foreigner_net_shares)}</td><td>${shares(i.investor_flows?.institution_net_shares)}</td></tr>`).join('')
      + '</tbody></table></div><p class="small muted">순위·수급은 토스 공식 조회 API 값입니다(수급은 국내 종목, 당일은 잠정치일 수 있음). "—"는 조회되지 않았다는 뜻입니다.</p></div>';
  }
  if (report.role === 'selector' && Array.isArray(report.ranking) && report.ranking.length) html += '<div class="briefing"><h4>후보 순위</h4><dl>' + report.ranking.map((item, index) => `<div><dt>${index + 1}. ${esc(item.symbol)}${item.symbol === report.symbol ? ' ✓' : ''}</dt><dd>${esc(item.reason)}</dd></div>`).join('') + '</dl></div>';
  if (Array.isArray(report.tasks) && report.tasks.length) html += '<div class="briefing"><h4>역할별 조사 지시</h4><dl>' + report.tasks.map(task => `<div><dt>${esc(names[task.role] || task.role)}</dt><dd>${esc(task.instruction)}</dd></div>`).join('') + '</dl></div>';
  if (Array.isArray(report.evidence) && report.evidence.length) {
    const retrieved = new Set((report.sources || []).map(source => safeUrl(source.url)).filter(Boolean));
    html += '<div class="evidence"><h4>조사 근거</h4><p class="muted small">이번 조사에서 조회한 자료와 연결합니다. 게시일과 내용은 원문에서 확인하세요.</p>' + report.evidence.map(item => {
      const url = safeUrl(item.source_url), linked = url && retrieved.has(url);
      const published = finite(item.published_at) ? dateTime(item.published_at) : item.published_at || '미확인';
      return `<article><p>${esc(item.claim)}</p><div class="evidence-meta"><span>보고서 표기 게시일 · ${esc(published)}</span>${item.retrieved_at ? '<span>자료 조회 · ' + dateTime(item.retrieved_at) + '</span>' : ''}${linked ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">조회 출처 ↗</a>` : '<span>조회된 출처 연결 없음</span>'}</div></article>`;
    }).join('') + '</div>';
  }
  return html + tradePlan(report);
}
function searchEntryFrame(run, report) {
  if (!run?.id || !report.role || !report.search_entry_point) return '';
  const src = '/api/search-entry/' + encodeURIComponent(run.id) + '/' + encodeURIComponent(report.role);
  // Provider HTML is only served inside the endpoint's CSP-sandboxed document.
  return `<iframe class="search-entry" src="${esc(src)}" title="Google 검색 제안" sandbox="allow-popups allow-popups-to-escape-sandbox" referrerpolicy="no-referrer" loading="lazy"></iframe>`;
}
function renderIntel(s) {
  const it = s.intel;
  if (!it) return;
  const parts = ['KR', 'US'].map(m => {
    const r = it.rankings[m];
    return `${m === 'KR' ? '국내' : '미국'} 순위 ${r ? `${Math.floor(r.age / 60)}분 전 (${r.kinds.length}종)` : '조회 전'}`;
  });
  parts.push(`국내 수급 ${it.flows.count}/${it.flows.tried}종목`);
  $('intel-status').textContent = '토스 공식 시장 정보 · ' + parts.join(' · ');
}
function renderLive(s) {
  const live = s.live;
  if (!live) return;
  const c = live.config, sh = live.shadow, lim = c.limits;
  $('live-lock').textContent = c.enabled ? '실거래 켜짐' : '잠김';
  $('live-reason').textContent = c.reason + (live.halt?.active ? ' · 정지: ' + (live.halt.reason || '') : '');
  const rows = [['1회 주문 금액', 'max_order'], ['하루 누적 주문 금액', 'max_daily_notional'], ['하루 주문 횟수', 'max_daily_orders'],
                ['하루 손실 한도', 'max_daily_loss'], ['종목당 보유 금액', 'max_position']];
  setHtml('live-limits', '<thead><tr><th></th><th>KRW</th><th>USD</th></tr></thead><tbody>'
    + rows.map(([label, key]) => `<tr><th>${label}</th><td>${Number(lim.KRW[key]).toLocaleString()}</td><td>${Number(lim.USD[key]).toLocaleString()}</td></tr>`).join('') + '</tbody>');
  $('live-limit-note').textContent = `허용 종목 ${lim.symbols.join(', ') || '없음'} · 미체결 ${lim.max_open_orders}건 · 지정가 허용 범위 ±${lim.price_band_pct}% · 매도(청산)는 금액·횟수 한도에 막히지 않습니다.`;
  const names = {'symbol-not-allowed': '허용 종목 아님', 'max-order': '1회 한도', 'max-daily-orders': '하루 횟수', 'max-daily-notional': '하루 금액',
                 'max-open-orders': '미체결 한도', 'max-position': '종목 보유 한도', 'daily-loss-halt': '일일 손실 정지', 'price-band': '가격 범위',
                 'exceeds-position': '보유 초과', 'invalid-intent': '주문 오류', 'halted': '거래 정지', 'unresolved-orders': '미확인 주문'};
  const blocked = Object.entries(sh.blocked_by || {}).map(([code, n]) => `${names[code] || esc(code)} ${n}건`).join(' · ') || '없음';
  const okPct = sh.total ? sh.would_submit / sh.total * 100 : 0;
  setHtml('live-shadow', `<div class="donut-wrap"><div class="donut ${sh.total ? '' : 'zero'}" data-pct="${okPct.toFixed(1)}" role="img" aria-label="전송 가능 ${sh.would_submit}건, 차단 ${sh.blocked}건"><span>${sh.would_submit}/${sh.total}<small>전송 가능</small></span></div>`
    + `<div class="legend"><span class="ok"><i></i>한도 안에서 전송 가능 <b>${sh.would_submit}건</b></span><span class="no"><i></i>한도에 막힘 <b>${sh.blocked}건</b></span></div></div>`
    + `<dl class="performance"><div><dt>기록된 주문</dt><dd>${sh.total}건 (매수 ${sh.by_side.BUY} · 매도 ${sh.by_side.SELL})</dd></div>`
    + `<div><dt>주요 차단 사유</dt><dd>${blocked}</dd></div><div><dt>전송 가능 금액 합계</dt><dd>KRW ${Number(sh.notional.KRW).toLocaleString()} · USD ${Number(sh.notional.USD).toLocaleString()}</dd></div></dl>`
    + (sh.blocked ? '<p class="small muted">막힌 주문이 많다면 모의투자의 주문 크기가 실거래용 한도보다 크다는 뜻입니다. 실거래 전에 한도와 주문 크기를 함께 조정해야 합니다.</p>' : ''));
  const plan = p => !p ? '—' : p.problems ? '계획 불가' : `손절 ${Number(p.stop_trigger).toLocaleString()} · 익절 ${Number(p.take_profit_trigger).toLocaleString()}`;
  setHtml('live-recent', (sh.recent || []).slice().reverse().map(r => `<tr><td>${clock(r.time)}</td><td>${esc(r.symbol)}</td><td>${r.side === 'BUY' ? '매수' : '매도'}${r.source === 'exit' ? '(청산)' : ''}</td><td>${r.quantity}</td><td>${Number(r.limit_price).toLocaleString()}</td><td>${Number(r.notional).toLocaleString()} ${esc(r.currency)}</td><td>${r.would_submit ? '전송 가능' : '<span class="down">차단</span>'}</td><td>${(r.blocked_by || []).map(code => names[code] || esc(code)).join(', ') || '—'}${r.suggested_quantity && !r.would_submit ? ` (허용 ${r.suggested_quantity}주)` : ''}</td><td>${plan(r.protective)}</td></tr>`).join('') || '<tr><td colspan="9" class="empty-cell">아직 기록된 그림자 주문이 없습니다.</td></tr>');
}
function renderEvalChart(ev) {
  document.querySelectorAll('#eval-tabs button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.h === evalHorizon)));
  const x = ev?.horizons?.[evalHorizon];
  if (!x || !x.scored) {
    setHtml('eval-chart', empty('아직 채점된 판단이 없습니다', `판단 후 ${evalHorizon}분이 지나면 AI와 단순 규칙의 성과 비교가 이곳에 표시됩니다.`, 'scale'));
    return;
  }
  const rows = [['AI 판단', x.ai_avg_net_pct, 'ai', `${x.scored}건`], ['항상 관망', x.always_hold_pct, '', ''], ['매번 매수', x.always_buy_avg_net_pct, '', '']]
    .concat(Object.entries(ruleNames).map(([key, label]) => [label, x.rules?.[key]?.avg_net_pct, '', x.rules?.[key]?.count ? `${x.rules[key].count}건` : '']))
    .filter(([, value]) => finite(value));
  const max = Math.max(.05, ...rows.map(([, value]) => Math.abs(value)));
  setHtml('eval-chart', rows.map(([label, value, cls, count]) => `<div class="hbar ${cls}"><span class="hb-label">${label}${count ? `<small>${count}</small>` : ''}</span><div class="hb-track"><i class="hb-fill ${value < 0 ? 'neg' : ''}" data-w="${Math.min(100, Math.abs(value) / max * 100).toFixed(1)}"></i></div><b class="hb-value ${value > 0 ? 'up' : value < 0 ? 'down' : ''}">${(value > 0 ? '+' : '') + value.toFixed(2)}%</b></div>`).join(''));
}
$('eval-tabs').onclick = e => {
  const b = e.target.closest('button[data-h]');
  if (b) { evalHorizon = b.dataset.h; if (state) renderEvalChart(state.evaluation); }
};
function renderEvaluation(s) {
  const ev = s.evaluation, list = s.evaluations || [];
  if (!ev) return;
  const signed = x => finite(x) ? `<span class="${x > 0 ? 'up' : x < 0 ? 'down' : ''}">${x > 0 ? '+' : ''}${x.toFixed(2)}%</span>` : '—';
  const rate = x => finite(x) ? `${x.toFixed(0)}%` : '—';
  $('eval-sample').textContent = `판단 ${ev.decisions}건 · 채점 대기 ${ev.pending}건`;
  renderEvalChart(ev);
  const rows = ['30', '60'].map(h => {
    const x = ev.horizons[h];
    return `<tr><th>${h}분 뒤</th><td>${x.scored}</td><td>${signed(x.ai_avg_net_pct)}</td><td>${signed(x.always_hold_pct)}</td><td>${signed(x.always_buy_avg_net_pct)}</td><td>${x.buy.count}건 · ${signed(x.buy.avg_net_pct)} · 적중 ${rate(x.buy.hit_rate_pct)}</td><td>${x.sell.count}건 · 회피 ${signed(x.sell.avg_avoided_pct)} · 적중 ${rate(x.sell.hit_rate_pct)}</td><td>${x.hold.count}건 · 놓친 상승 ${rate(x.hold.missed_gain_rate_pct)}</td><td>${x.selector.count ? `${signed(x.selector.chosen_abs_move_pct)} vs ${signed(x.selector.others_abs_move_pct)} · 더 큰 움직임 ${rate(x.selector.bigger_mover_rate_pct)}` : '—'}</td></tr>`;
  }).join('');
  const engines = Object.entries(ev.engines).map(([name, x]) => `${esc(name)} ${x.decisions}건`).join(' · ') || '—';
  const ruleRows = Object.entries(ruleNames).map(([name, label]) => `<tr><th>${label}</th>` + ['30', '60'].map(h => {
    const r = ev.horizons[h].rules?.[name];
    return r && r.count ? `<td>${r.count}건 · 매매 ${r.trades}</td><td>${signed(r.avg_net_pct)}</td><td>${signed(r.ai_same_avg_net_pct)}</td>` : '<td colspan="3" class="muted">—</td>';
  }).join('') + '</tr>').join('');
  setHtml('eval-summary', `<div class="table-wrap"><table class="eval-table"><thead><tr><th>기준</th><th>채점</th><th>AI 판단(비용 차감)</th><th>항상 관망</th><th>매번 매수</th><th>매수</th><th>매도</th><th>관망</th><th>종목 선정(변동폭)</th></tr></thead><tbody>${rows}</tbody></table></div>`
    + `<p class="small muted eval-note">${ev.enough_sample ? '' : `⚠ 60분 채점 ${ev.min_sample}건 미만: 운과 실력을 구분하기 어려운 표본입니다. `}AI 판단 = 매수는 수익률, 매도는 하락 회피, 관망은 0%로 계산한 평균입니다. 판단 AI: ${engines}</p>`
    + `<div class="table-wrap"><table class="eval-table"><thead><tr><th rowspan="2">규칙 기반 비교</th><th colspan="3">30분 뒤</th><th colspan="3">60분 뒤</th></tr><tr><th>채점</th><th>규칙(비용 차감)</th><th>AI(같은 판단)</th><th>채점</th><th>규칙(비용 차감)</th><th>AI(같은 판단)</th></tr></thead><tbody>${ruleRows}</tbody></table></div><p class="small muted eval-note">같은 판단 시점·같은 가격·같은 비용으로 기계적 규칙을 채점한 값입니다. 규칙은 완료된 1분봉으로만 계산하며 튜닝하지 않은 기본값입니다. <strong>AI가 이 단순 규칙보다 꾸준히 낫지 않다면 AI 판단에 의존할 이유가 없습니다.</strong></p>`);
  const stance = {BUY:'매수',SELL:'매도',HOLD:'관망'}, by = {ai:'AI',user:'직접',server:'순서'};
  const cell = (e, h) => { const o = e.outcomes[h]; if (!o) return '<span class="muted">대기</span>'; if (o.missed) return '<span class="muted">누락</span>'; return signed(o.returns?.[e.symbol]) + (o.at_close ? ' <span class="muted small">마감</span>' : ''); };
  setHtml('eval-recent', list.slice().reverse().map(e => `<tr><td>${clock(e.time)}</td><td>${esc(e.symbol)}</td><td>${stance[e.stance] || esc(e.stance)}</td><td>${by[e.selected_by] || esc(e.selected_by)}</td><td>${esc(e.engine.split(' · ')[0])}</td><td>${esc(e.action)}</td><td>${cell(e, '30')}</td><td>${cell(e, '60')}</td></tr>`).join('') || '<tr><td colspan="8" class="empty-cell">아직 기록된 판단이 없습니다.</td></tr>');
}
function renderTeam(s) {
  const run = s.runs.at(-1), roles = roleList(s), intraday = s.strategy_mode === 'intraday';
  $('research-flow').textContent = intraday ? '플래너가 조사 지시 → 기업·차트·뉴스 분석가가 병렬 조사 → 반대 검토자가 약점 확인 → 디렉터가 매매 계획 결정' : '기업·차트·뉴스 분석 → 반대 검토 → 디렉터의 매매 의견';
  $('team').classList.toggle('six-roles',roles.length === 6);
  $('team-context').textContent = run ? `${s.instruments.find(i => i.symbol === run.symbol)?.name || run.symbol} · ${clock(run.time)} · ${({running:'분석 중',completed:'분석 완료',cancelled:'중지됨',error:'확인 필요'})[run.status] || run.status}` : '실행하면 역할별 분석이 이곳에 쌓입니다.';
  setHtml('team', roles.map(({id:role,name},index) => {
    const done = run?.reports.find(r => r.role === role), active = run?.status === 'running' && (run.active_role === role || run.active_roles?.includes(role));
    return `<article class="agent ${active ? 'active' : done ? 'done' : ''}"><div class="agent-icon">${icon(roleIcons[role] || 'spark')}</div><span class="agent-no">0${index+1}</span><h3>${esc(name)}</h3><p>${active ? '분석 중…' : done ? '검토 완료' : run?.status === 'cancelled' ? '중지됨' : '대기 중'}</p></article>`;
  }).join(''));
  $('sizing-summary').hidden = !run?.sizing;
  if (run?.sizing) setHtml('sizing-summary', `<div class="sizing-head"><h3>수량 산정</h3><span>최종 제안 <b>${quantity(run.sizing.quantity)}</b></span></div>${sizingDetails(run.sizing,s.instruments.find(i => i.symbol === run.symbol)?.currency)}<p class="muted small">목표·위험·매수 가능 수량 안에서 정수 주식으로 계산합니다. 손절 기준은 체결 가격을 보장하지 않습니다.</p>`);
  const nextKey = JSON.stringify(run);
  if (reportKey !== nextKey) {
    const opened = new Set([...$('reports').querySelectorAll('details[open]')].map(d => d.dataset.role));
    reportKey = nextKey;
    $('reports').innerHTML = (run?.reports || []).map(r => `<details data-role="${esc(r.role)}" ${opened.has(r.role) || r.role === 'director' || r.role === 'planner' || r.role === 'selector' ? 'open' : ''}><summary>${esc(r.name || names[r.role])} · ${r.role === 'selector' ? '선정 · ' + esc(r.symbol) : r.role === 'planner' ? '조사 브리핑' : esc(({BUY:'매수 의견',SELL:'매도 의견',HOLD:'관망 의견'})[r.stance] || '분석 보고서')}</summary><div class="report-body"><p>${esc(r.summary)}</p>${reportExtras(r)}<ul>${(r.risks || []).map(x => `<li>${esc(x)}</li>`).join('')}</ul>${(r.sources || []).filter(x => safeUrl(x.url)).map(x => `<a href="${esc(safeUrl(x.url))}" target="_blank" rel="noopener noreferrer">${esc(x.title || x.url)} ↗</a>`).join('')}${searchEntryFrame(run,r)}<div class="report-model">${esc(r.engine)} · ${clock(r.time)}${r.usage?.total_tokens ? ' · ' + Number(r.usage.total_tokens).toLocaleString() + ' tokens' : ''}</div></div></details>`).join('') + (run?.error ? `<div class="notice error">${esc(run.error)}</div>` : '') + (run?.blocked ? `<div class="notice">${esc(run.blocked)}</div>` : '');
  }
}
function drawChart() {
  if (!state) return;
  const el = $('equity-chart'), c = $('chart-currency').value;
  const data = state.history.filter(d => finite(d[c]) && finite(d.time)).slice(-120);
  const w = Math.max(240, el.clientWidth), h = 175, left = 58, right = 10, top = 14, bottom = 30;
  $('chart-title').textContent = c === 'KRW' ? '원화 평가자산' : '달러 평가자산';
  el.setAttribute('viewBox', `0 0 ${w} ${h}`);
  if (data.length < 2) {
    setHtml('equity-chart', `<text class="axis" x="${w/2}" y="90" text-anchor="middle" font-size="13">기록을 수집하고 있습니다</text>`);
    $('chart-note').textContent = '현재 실험의 평가자산 기록이 쌓이면 표시합니다.';
    return;
  }
  let lo = Math.min(...data.map(d => d[c])), hi = Math.max(...data.map(d => d[c]));
  const pad = Math.max((hi-lo)*.2, Math.abs(hi)*.0002, c==='KRW'?1:.01);
  lo -= pad; hi += pad;
  const x = j => left+j/(data.length-1)*(w-left-right), y = v => top+(hi-v)/(hi-lo)*(h-top-bottom), base = h-bottom;
  let out = `<defs><linearGradient id="eq-fill" gradientUnits="userSpaceOnUse" x1="0" y1="${top}" x2="0" y2="${base}"><stop offset="0" class="st-a"/><stop offset="1" class="st-b"/></linearGradient><linearGradient id="eq-line" gradientUnits="userSpaceOnUse" x1="${left}" y1="0" x2="${w-right}" y2="0"><stop offset="0" class="st-l1"/><stop offset="1" class="st-l2"/></linearGradient></defs>`;
  for (let j=0; j<3; j++) {
    const v = lo+(hi-lo)*j/2, py = y(v);
    out += `<line class="grid" x1="${left}" y1="${py}" x2="${w-right}" y2="${py}"/><text class="axis" x="${left-9}" y="${py+4}" text-anchor="end">${new Intl.NumberFormat('ko-KR',{notation:'compact',maximumFractionDigits:1}).format(v)}</text>`;
  }
  const pts = data.map((d,i) => [x(i), y(d[c])]), line = smoothPath(pts, top, base), last = pts.at(-1);
  out += `<path class="eq-area" fill="url(#eq-fill)" d="${line} L${last[0].toFixed(1)},${base} L${pts[0][0].toFixed(1)},${base} Z"/><path class="eq-line" stroke="url(#eq-line)" d="${line}"/><circle class="eq-dot" cx="${last[0].toFixed(1)}" cy="${last[1].toFixed(1)}" r="4.5"/>`
    + `<text class="axis" x="${left}" y="${h-6}">${clock(data[0].time).slice(0,5)}</text><text class="axis" x="${w-right}" y="${h-6}" text-anchor="end">${clock(data.at(-1).time).slice(0,5)}</text>`;
  setHtml('equity-chart', out);
  $('chart-note').textContent = '단위 ' + c + ' · 최근 ' + data.length + '개 기록 · 보유 종목은 마지막 수신 가격으로 평가합니다.';
}
function drawSpark(id, c) {
  const data = state.history.filter(d => finite(d[c])).slice(-72);
  if (data.length < 2) { setHtml(id, ''); return; }
  const w = 200, h = 52, pad = 5, values = data.map(d => d[c]), lo = Math.min(...values), hi = Math.max(...values);
  const span = Math.max(hi - lo, Math.abs(hi) * .0004, 1e-9);
  const mid = (hi + lo) / 2;   // centre the range, so a flat (or nearly flat) series sits in the middle of the tile
  const pts = data.map((d, i) => [i / (data.length - 1) * w, h / 2 + (mid - d[c]) / span * (h - 2 * pad)]), line = smoothPath(pts, pad, h - pad);
  // userSpaceOnUse: a perfectly flat line has a zero-height bounding box, and an objectBoundingBox gradient would paint nothing.
  setHtml(id, `<defs><linearGradient id="${id}-fill" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="0" y2="${h}"><stop offset="0" class="sp-a"/><stop offset="1" class="sp-b"/></linearGradient><linearGradient id="${id}-line" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="${w}" y2="0"><stop offset="0" class="sp-l1"/><stop offset="1" class="sp-l2"/></linearGradient></defs>`
    + `<path class="sp-area" fill="url(#${id}-fill)" d="${line} L${w},${h} L0,${h} Z"/><path class="sp-line" stroke="url(#${id}-line)" d="${line}"/>`);
}
new ResizeObserver(() => drawChart()).observe($('equity-chart'));
reload();
setInterval(() => { if (loggedIn) reload(); },2000);
