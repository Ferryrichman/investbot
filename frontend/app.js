// HK 財技股 Monitor frontend
// 純 client-side filter — 讀 data.json,跟 user filter 即時更新

const CHECK_LABELS = {
  mcap: '市值',
  raise: 'IPO集資',
  recent_ipo: '近4年',
  concentration: '大莊 75%',
  brokers: '券商<120',
  turnover: '成交<100萬',
  sideways: '橫行',
  sponsor: '保薦人記錄',
};
const CHECK_ORDER = ['mcap','raise','recent_ipo','concentration','brokers','turnover','sideways','sponsor'];

let RAW = null;

// ============================================================
// Watchlist - persisted in localStorage
// ============================================================
const WATCHLIST_KEY = 'hk_screener_watchlist';
function loadWatchlist() {
  try { return new Set(JSON.parse(localStorage.getItem(WATCHLIST_KEY) || '[]')); }
  catch { return new Set(); }
}
function saveWatchlist(s) {
  localStorage.setItem(WATCHLIST_KEY, JSON.stringify([...s]));
}
let WATCHLIST = loadWatchlist();

function toggleWatch(code) {
  if (WATCHLIST.has(code)) WATCHLIST.delete(code);
  else WATCHLIST.add(code);
  saveWatchlist(WATCHLIST);
  updateWatchlistCount();
  render();
}
function updateWatchlistCount() {
  document.getElementById('watchlist_count').textContent = WATCHLIST.size;
}

// ============================================================
// Sort state
// ============================================================
let SORT = { col: 'score', dir: 'desc' };  // default

function cmpVal(a, b, col) {
  let va, vb;
  if (col === 'watchlist') {
    va = WATCHLIST.has(a.code) ? 1 : 0;
    vb = WATCHLIST.has(b.code) ? 1 : 0;
  } else if (col === 'sponsor_hit_rate') {
    va = a.sponsor_hit_rate ?? -1;
    vb = b.sponsor_hit_rate ?? -1;
  } else if (col === 'code') {
    va = parseInt(a.code, 10) || 0;
    vb = parseInt(b.code, 10) || 0;
  } else {
    va = a[col]; vb = b[col];
  }
  if (va == null && vb == null) return 0;
  if (va == null) return 1;   // nulls last
  if (vb == null) return -1;
  if (typeof va === 'number' && typeof vb === 'number') return va - vb;
  return String(va).localeCompare(String(vb), 'zh-Hant');
}

function applySort(arr) {
  const dir = SORT.dir === 'asc' ? 1 : -1;
  return [...arr].sort((a, b) => {
    const c = cmpVal(a, b, SORT.col);
    if (c !== 0) return c * dir;
    // tiebreaker: score desc, then code asc
    const s = (b.score || 0) - (a.score || 0);
    if (s !== 0) return s;
    return parseInt(a.code) - parseInt(b.code);
  });
}

function setSort(col) {
  if (SORT.col === col) {
    SORT.dir = SORT.dir === 'asc' ? 'desc' : 'asc';
  } else {
    SORT.col = col;
    // numeric columns default desc, text default asc
    SORT.dir = ['code', 'name', 'board', 'industry', 'listing_date'].includes(col) ? 'asc' : 'desc';
  }
  updateSortIndicator();
  render();
}

function updateSortIndicator() {
  document.querySelectorAll('th[data-sort]').forEach(th => {
    th.classList.remove('sorted-asc', 'sorted-desc');
    if (th.dataset.sort === SORT.col) {
      th.classList.add(SORT.dir === 'asc' ? 'sorted-asc' : 'sorted-desc');
    }
  });
}

async function load() {
  try {
    const r = await fetch('data.json?t=' + Date.now());
    RAW = await r.json();
  } catch (e) {
    document.getElementById('stock_count').textContent = 'load failed';
    return;
  }
  document.getElementById('generated_at').textContent = new Date(RAW.generated_at).toLocaleString();
  document.getElementById('stock_count').textContent = RAW.stocks.length;
  render();
}

function fmtHKD(n) {
  if (n == null) return '—';
  if (n >= 1e8) return (n/1e8).toFixed(2) + '億';
  if (n >= 1e4) return (n/1e4).toFixed(1) + '萬';
  return n.toLocaleString();
}
function fmtPct(n) {
  if (n == null) return '—';
  return n.toFixed(1) + '%';
}
function fmtTurnover(n) {
  if (n == null) return '—';
  if (n >= 1e8) return (n/1e8).toFixed(2) + '億';
  if (n >= 1e4) return (n/1e4).toFixed(0) + '萬';
  return n.toLocaleString();
}

function getActiveFilters() {
  return {
    hardPass: document.getElementById('f_hard_pass').checked,
    hideUnknownRaise: document.getElementById('f_hide_unknown_raise').checked,
    boards: [...document.querySelectorAll('.board_filter:checked')].map(c => c.value),
    requiredChecks: [...document.querySelectorAll('.check_filter:checked')].map(c => c.value),
    requiredTags: [...document.querySelectorAll('.tag_filter:checked')].map(c => c.value),
    excludeHshare: document.getElementById('exclude_hshare').checked,
    excludeCh21: document.getElementById('exclude_ch21').checked,
    watchlistOnly: document.getElementById('watchlist_only').checked,
  };
}

function applyFilters(stocks, f) {
  return stocks.filter(s => {
    // Watchlist-only mode takes precedence
    if (f.watchlistOnly && !WATCHLIST.has(s.code)) return false;
    // Board
    if (!f.boards.includes(s.board)) return false;
    // Exclusions
    if (f.excludeHshare && (s.stock_type === 'H Share' || s.stock_type === 'Red Chip')) return false;
    if (f.excludeCh21 && s.is_chapter21) return false;
    // Hide unknown raise
    if (f.hideUnknownRaise && s.checks.raise === null) return false;
    // Required checks (all must pass)
    for (const c of f.requiredChecks) {
      if (s.checks[c] !== true) return false;
    }
    // Hard-pass mode: all KNOWN checks must be true
    if (f.hardPass) {
      const known = Object.values(s.checks).filter(v => v !== null);
      const passed = known.filter(v => v === true);
      if (passed.length < known.length) return false;
    }
    // Required tags (any match)
    if (f.requiredTags.length) {
      const t = s.industry_tags || [];
      const ok = f.requiredTags.some(rt => t.includes(rt));
      if (!ok) return false;
    }
    return true;
  });
}

function checkSymbol(v) {
  if (v === true) return '<span class="check pass">✓</span>';
  if (v === false) return '<span class="check fail">✗</span>';
  return '<span class="check unknown">?</span>';
}

function render() {
  if (!RAW) return;
  const f = getActiveFilters();
  const filtered = applySort(applyFilters(RAW.stocks, f));
  const tbody = document.getElementById('stock_tbody');
  const empty = document.getElementById('empty_state');
  tbody.innerHTML = '';

  if (!filtered.length) {
    empty.style.display = 'block';
    return;
  }
  empty.style.display = 'none';

  const frag = document.createDocumentFragment();
  for (const s of filtered) {
    const tr = document.createElement('tr');
    tr.dataset.code = s.code;
    const scoreClass = s.score >= 6 ? 'score-high' : s.score >= 4 ? 'score-mid' : 'score-low';
    const checksHtml = CHECK_ORDER.map(c => {
      const v = s.checks[c];
      return `<span title="${CHECK_LABELS[c]}: ${v===true?'pass':v===false?'fail':'?'}">${checkSymbol(v)}</span>`;
    }).join('');
    const tagsHtml = (s.industry_tags || []).map(t => `<span class="tag ${t}">${t.replace('_friendly','').replace('_',' ')}</span>`).join('');
    const sponsorTxt = s.sponsor
      ? `${s.sponsor.substring(0,18)} ${s.sponsor_hit_rate != null ? `(${(s.sponsor_hit_rate*100).toFixed(0)}%)` : ''}`
      : '—';
    const starred = WATCHLIST.has(s.code);

    tr.innerHTML = `
      <td class="star-cell"><span class="star ${starred ? 'active' : ''}" data-code="${s.code}" title="Watchlist">${starred ? '★' : '☆'}</span></td>
      <td class="score ${scoreClass}">${s.score}/${s.score_max}</td>
      <td class="code">${s.code}</td>
      <td class="name" title="${s.name_zh || s.name || ''}">${s.name || ''}${getAnomalyBadges(s)}</td>
      <td class="board">${s.board}</td>
      <td class="num">${fmtHKD(s.market_cap_hkd)}</td>
      <td class="date">${s.listing_date || '—'}</td>
      <td class="num">${fmtHKD(s.raise_amount_hkd)}</td>
      <td>${sponsorTxt}</td>
      <td class="num">${fmtPct(s.top10_pct)}</td>
      <td class="num">${s.broker_count ?? '—'}</td>
      <td class="num">${fmtTurnover(s.avg_turnover)}</td>
      <td class="num">${fmtPct(s.range_pct)}</td>
      <td>${tagsHtml}<br><small style="color:#7d8590">${(s.industry||'').substring(0,28)}</small></td>
      <td><div class="checks">${checksHtml}</div></td>
    `;
    // Row click → detail (except star click)
    tr.addEventListener('click', (e) => {
      if (e.target.classList.contains('star')) {
        e.stopPropagation();
        toggleWatch(s.code);
      } else {
        showDetail(s);
      }
    });
    frag.appendChild(tr);
  }
  tbody.appendChild(frag);
  document.getElementById('stock_count').textContent = `${filtered.length} / ${RAW.stocks.length}`;
}

function renderSparkline(spark, width = 600, height = 80) {
  if (!spark || spark.length < 2) return '<div style="color:#7d8590">冇足夠歷史數據(< 2 個 snapshot)</div>';
  const top10s = spark.map(p => p.top10);
  const min = Math.min(...top10s), max = Math.max(...top10s);
  const range = max - min || 1;
  const xStep = width / (spark.length - 1);
  const path = spark.map((p, i) =>
    `${i === 0 ? 'M' : 'L'} ${(i * xStep).toFixed(1)} ${(height - ((p.top10 - min) / range) * (height - 10) - 5).toFixed(1)}`
  ).join(' ');
  // Threshold line at 75%
  let thresholdLine = '';
  if (max >= 75 || min <= 75) {
    const y = (height - ((75 - min) / range) * (height - 10) - 5);
    thresholdLine = `<line x1="0" y1="${y}" x2="${width}" y2="${y}" stroke="#f85149" stroke-dasharray="3,3" stroke-width="0.8" opacity="0.5"/><text x="4" y="${y - 2}" fill="#f85149" font-size="9">75%</text>`;
  }
  const firstDate = spark[0].d, lastDate = spark[spark.length-1].d;
  const firstVal = top10s[0].toFixed(1), lastVal = top10s[top10s.length-1].toFixed(1);
  return `
    <svg class="sparkline-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
      ${thresholdLine}
      <path d="${path}" stroke="#58a6ff" stroke-width="1.5" fill="none"/>
      ${spark.map((p, i) => `<circle cx="${(i * xStep).toFixed(1)}" cy="${(height - ((p.top10 - min) / range) * (height - 10) - 5).toFixed(1)}" r="1.5" fill="#58a6ff"/>`).join('')}
    </svg>
    <div style="display:flex;justify-content:space-between;font-size:10px;color:#7d8590;margin-top:4px">
      <span>${firstDate} (${firstVal}%)</span>
      <span>${lastDate} (${lastVal}%)</span>
    </div>`;
}

function fmtDelta(v) {
  if (v == null) return '<span class="delta-neutral">—</span>';
  const cls = v > 0.5 ? 'delta-positive' : v < -0.5 ? 'delta-negative' : 'delta-neutral';
  const sign = v > 0 ? '+' : '';
  return `<span class="${cls}">${sign}${v.toFixed(1)}</span>`;
}

function renderTrend(t) {
  if (!t || !t.sparkline || t.sparkline.length < 1) {
    return '<div class="sparkline-block"><h4>CCASS 時序</h4><div style="color:#7d8590">冇歷史數據(需要 daily scrape 累積)</div></div>';
  }
  const d = t.deltas || {};
  let brokers = '';
  if (t.broker_changes_30d && t.broker_changes_30d.length) {
    brokers = `
      <div class="broker-changes">
        <h4 style="margin:8px 0 4px">30 日 broker 倉位變化(top 5)</h4>
        <table>
          ${t.broker_changes_30d.slice(0, 5).map(c => `
            <tr>
              <td style="font-family:monospace">${c.id}</td>
              <td style="color:#7d8590">${c.name.substring(0, 35)}</td>
              <td style="text-align:right">${c.before_pct.toFixed(1)}% → ${c.after_pct.toFixed(1)}%</td>
              <td style="text-align:right">${fmtDelta(c.delta)}</td>
            </tr>`).join('')}
        </table>
      </div>`;
  }
  let anoms = '';
  if (t.anomalies && t.anomalies.length) {
    const labels = {
      top10_rise_30d: '🟢 30日累積',
      top10_drop_30d: '🔴 30日派貨',
      broker_new: '🆕 新莊上場',
      broker_accumulation: '🟢 莊家加碼',
    };
    anoms = `
      <div style="margin-top:8px">
        <h4 style="margin:8px 0 4px">最近異常</h4>
        ${t.anomalies.slice(0, 5).map(a => {
          const lbl = labels[a.anomaly_type] || a.anomaly_type;
          return `<div style="font-size:11px;padding:2px 0">${a.detected_date} · ${lbl} · severity ${(a.severity*100).toFixed(0)}%</div>`;
        }).join('')}
      </div>`;
  }
  return `
    <div class="sparkline-block">
      <h4>CCASS top10 % 走勢</h4>
      ${renderSparkline(t.sparkline)}
      <div style="display:flex;gap:16px;margin-top:8px;font-size:11px">
        <div>7d: ${fmtDelta(d.top10_d7d)}%</div>
        <div>30d: ${fmtDelta(d.top10_d30d)}%</div>
        <div>90d: ${fmtDelta(d.top10_d90d)}%</div>
      </div>
      ${brokers}
      ${anoms}
    </div>`;
}

function getAnomalyBadges(s) {
  const t = s.trend;
  if (!t || !t.anomalies || !t.anomalies.length) return '';
  const recent = t.anomalies.slice(0, 3);
  const map = {
    top10_rise_30d: { cls: 'accumulation', txt: '↑食貨' },
    top10_drop_30d: { cls: 'distribution', txt: '↓派貨' },
    broker_new: { cls: 'new_broker', txt: '新莊' },
    broker_accumulation: { cls: 'accumulation', txt: '加碼' },
  };
  const seen = new Set();
  return recent.filter(a => {
    if (seen.has(a.anomaly_type)) return false;
    seen.add(a.anomaly_type);
    return true;
  }).map(a => {
    const m = map[a.anomaly_type];
    return m ? `<span class="anomaly-badge ${m.cls}" title="${a.anomaly_type}">${m.txt}</span>` : '';
  }).join('');
}

function showDetail(s) {
  const html = `
    <h2>${s.code} · ${s.name}</h2>
    <div style="color:#7d8590;margin-bottom:16px">${s.name_zh || ''} · ${s.board} · ${s.industry || ''}</div>
    <div class="row"><div class="lbl">市值</div><div class="val">${fmtHKD(s.market_cap_hkd)}</div></div>
    <div class="row"><div class="lbl">上市日期</div><div class="val">${s.listing_date || '—'}</div></div>
    <div class="row"><div class="lbl">IPO 價</div><div class="val">${s.ipo_price_hkd ? s.ipo_price_hkd.toFixed(3) : '—'}</div></div>
    <div class="row"><div class="lbl">IPO 集資</div><div class="val">${fmtHKD(s.raise_amount_hkd)}</div></div>
    <div class="row"><div class="lbl">保薦人</div><div class="val">${s.sponsor || '—'}</div></div>
    <div class="row"><div class="lbl">保薦人記錄</div><div class="val">${s.sponsor_hit_rate != null ? `${s.sponsor_pumped}/${s.sponsor_total} = ${(s.sponsor_hit_rate*100).toFixed(0)}%` : '—'}</div></div>
    <div class="row"><div class="lbl">當前股價</div><div class="val">$${s.last_close ? s.last_close.toFixed(3) : '—'}</div></div>
    <div class="row"><div class="lbl">300日 高/低</div><div class="val">${s.range_high?.toFixed(3) || '—'} / ${s.range_low?.toFixed(3) || '—'} (range ${fmtPct(s.range_pct)})</div></div>
    <div class="row"><div class="lbl">平均成交</div><div class="val">${fmtTurnover(s.avg_turnover)}/日</div></div>
    <div class="row"><div class="lbl">大莊 (top1)</div><div class="val">${fmtPct(s.top1_pct)}</div></div>
    <div class="row"><div class="lbl">前10持有人</div><div class="val">${fmtPct(s.top10_pct)}</div></div>
    <div class="row"><div class="lbl">CCASS 券商數</div><div class="val">${s.broker_count ?? '—'} (snapshot ${s.ccass_date || '—'})</div></div>
    <div class="row"><div class="lbl">行業標籤</div><div class="val">${(s.industry_tags||[]).join(', ') || '—'}</div></div>
    ${renderTrend(s.trend)}
    <div style="margin-top:20px;display:flex;gap:8px">
      <a href="https://www3.hkexnews.hk/sdw/search/searchsdw.aspx?stockcode=${s.code.padStart(5,'0')}" target="_blank" style="color:#58a6ff">CCASS</a>
      &nbsp;·&nbsp;
      <a href="https://finance.yahoo.com/quote/${s.code.padStart(4,'0')}.HK" target="_blank" style="color:#58a6ff">Yahoo</a>
      &nbsp;·&nbsp;
      <a href="https://www.aastocks.com/tc/stocks/quote/detail-quote.aspx?symbol=${s.code.padStart(5,'0')}" target="_blank" style="color:#58a6ff">AAStocks</a>
      &nbsp;·&nbsp;
      <a href="https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=zh&stockId=${s.code}" target="_blank" style="color:#58a6ff">HKEXnews</a>
    </div>
  `;
  document.getElementById('detail_content').innerHTML = html;
  document.getElementById('detail_modal').showModal();
}

// Wire up filter listeners
function wireFilters() {
  document.querySelectorAll('input[type="checkbox"]').forEach(c =>
    c.addEventListener('change', render));
  document.getElementById('reset_btn').addEventListener('click', () => {
    document.querySelectorAll('input[type="checkbox"]').forEach(c => {
      c.checked = c.id === 'f_hard_pass' || c.id === 'exclude_hshare' || c.id === 'exclude_ch21'
                  || c.classList.contains('board_filter')
                  || (c.classList.contains('check_filter') && c.value === 'mcap');
    });
    render();
  });
  document.getElementById('close_detail').addEventListener('click', () =>
    document.getElementById('detail_modal').close());

  // Sortable headers
  document.querySelectorAll('th[data-sort]').forEach(th => {
    th.addEventListener('click', () => setSort(th.dataset.sort));
  });
  updateSortIndicator();

  // Watchlist buttons
  document.getElementById('export_watchlist').addEventListener('click', () => {
    const codes = [...WATCHLIST].sort((a, b) => parseInt(a) - parseInt(b));
    const text = codes.join(',');
    navigator.clipboard.writeText(text).then(() =>
      alert(`已 copy ${codes.length} 隻股票代碼到剪貼簿:\n${text}`)
    ).catch(() => prompt('Copy 失敗,自己 copy:', text));
  });
  document.getElementById('import_watchlist').addEventListener('click', () => {
    const input = prompt('貼入股票代碼(逗號分隔,eg. 2347,1284,8549):');
    if (!input) return;
    const codes = input.split(/[,\s]+/).map(c => c.trim()).filter(c => /^\d+$/.test(c));
    codes.forEach(c => WATCHLIST.add(c));
    saveWatchlist(WATCHLIST);
    updateWatchlistCount();
    render();
    alert(`已加入 ${codes.length} 隻`);
  });
  document.getElementById('clear_watchlist').addEventListener('click', () => {
    if (!WATCHLIST.size) return;
    if (confirm(`清空 ${WATCHLIST.size} 隻 watchlist?`)) {
      WATCHLIST.clear();
      saveWatchlist(WATCHLIST);
      updateWatchlistCount();
      render();
    }
  });
  updateWatchlistCount();
}

wireFilters();
load();
