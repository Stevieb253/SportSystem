// main.js — Tab switching, table sorting, filtering, search, date nav.

// ── Tab switching ─────────────────────────────────────────────────────────────
function showTab(tabName, btnElement) {
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
  const tab = document.getElementById('tab-' + tabName);
  if (tab) tab.classList.add('active');
  if (btnElement) btnElement.classList.add('active');
}

// ── Table sorting ─────────────────────────────────────────────────────────────
const _sortState = {};

function sortTable(tableId, colIndex) {
  const table = document.getElementById(tableId);
  if (!table) return;
  const tbody = table.querySelector('tbody');
  const rows  = Array.from(tbody.querySelectorAll('tr:not([style*="display: none"])'));
  const key   = tableId + '_' + colIndex;
  const asc   = !_sortState[key];
  _sortState[key] = asc;

  rows.sort((a, b) => {
    const cellA = a.cells[colIndex];
    const cellB = b.cells[colIndex];
    // Prefer data-sort-val (stacked stat cells) over raw textContent
    const aText = (cellA?.dataset.sortVal ?? cellA?.textContent ?? '').trim();
    const bText = (cellB?.dataset.sortVal ?? cellB?.textContent ?? '').trim();
    const aNum  = parseFloat(aText.replace(/[^0-9.\-]/g, ''));
    const bNum  = parseFloat(bText.replace(/[^0-9.\-]/g, ''));
    if (!isNaN(aNum) && !isNaN(bNum)) return asc ? aNum - bNum : bNum - aNum;
    return asc ? aText.localeCompare(bText) : bText.localeCompare(aText);
  });
  rows.forEach(r => tbody.appendChild(r));

  // Update sort-direction arrows via data attribute (CSS ::after handles rendering)
  table.querySelectorAll('th').forEach((th, i) => {
    if (i === colIndex) {
      th.dataset.sortDir = asc ? 'asc' : 'desc';
    } else {
      delete th.dataset.sortDir;
    }
  });
}

// ── Unified filter state ──────────────────────────────────────────────────────
// Holds the active filter value for each tab + dimension.
// e.g. _filterState.hit.verdict = 'YES', _filterState.hr.team = 'LAD'
const _filterState = {
  hit: { verdict: '', team: '', hand: '', slot: '' },
  hr:  { verdict: '', team: '', hand: '', slot: '' },
};

function toggleFilter(tab, dimension, val, btn) {
  _filterState[tab][dimension] = val;

  // Mark the clicked button active within its sibling group
  if (btn) {
    const group = btn.closest('.filter-btns');
    if (group) group.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
  }

  applyFilters(tab);
}

function applyFilters(tab) {
  const state = _filterState[tab];
  const search = (document.getElementById(tab + '-search')?.value || '').toLowerCase();
  const teamSel = document.getElementById(tab + '-team')?.value || '';

  if (tab === 'hit') {
    const table = document.getElementById('hit-table');
    if (!table) return;
    table.querySelectorAll('tbody tr').forEach(row => {
      const show = _rowMatches(row, state, search, teamSel, 'table');
      row.style.display = show ? '' : 'none';
    });
  } else {
    const grid = document.getElementById('hr-grid');
    if (!grid) return;
    grid.querySelectorAll('.hr-card').forEach(card => {
      const show = _rowMatches(card, state, search, teamSel, 'card');
      card.style.display = show ? '' : 'none';
    });
  }
}

function _rowMatches(el, state, search, teamSel, type) {
  // Verdict
  if (state.verdict && el.dataset.verdict !== state.verdict) return false;

  // Team (from select or state)
  const team = teamSel || state.team;
  if (team && el.dataset.team !== team) return false;

  // Hand
  if (state.hand && el.dataset.hand !== state.hand) return false;

  // Lineup slot
  if (state.slot) {
    const pos = parseInt(el.dataset.pos || '0', 10);
    if (state.slot === 'top' && (pos < 1 || pos > 3)) return false;
    if (state.slot === 'mid' && (pos < 4 || pos > 6)) return false;
    if (state.slot === 'bot' && (pos < 7 || pos > 9)) return false;
  }

  // Text search (player name)
  if (search) {
    const nameEl = type === 'table'
      ? el.cells[0]?.textContent
      : el.querySelector('.hr-player-name')?.textContent;
    if (!(nameEl || '').toLowerCase().includes(search)) return false;
  }

  return true;
}

// ── Populate team dropdowns from row data ─────────────────────────────────────
function _populateTeamDropdown(tab, selector) {
  const sel = document.getElementById(tab + '-team');
  if (!sel) return;
  const teams = new Set();
  document.querySelectorAll(selector).forEach(el => {
    const t = el.dataset.team;
    if (t) teams.add(t);
  });
  const sorted = Array.from(teams).sort();
  sorted.forEach(t => {
    const opt = document.createElement('option');
    opt.value = t;
    opt.textContent = t;
    sel.appendChild(opt);
  });
}

// Run once DOM is ready (script is at bottom of body so DOM is built)
_populateTeamDropdown('hit', '#hit-table tbody tr');
_populateTeamDropdown('hr',  '#hr-grid .hr-card');

// ── Legacy wrappers (kept so any inline onclick still works) ──────────────────
function filterTable(tableId, verdict, btn) {
  const tab = tableId === 'hit-table' ? 'hit' : 'hr';
  toggleFilter(tab, 'verdict', verdict === 'ALL' ? '' : verdict, btn);
}
function searchTable(tableId, query) {
  const tab = tableId === 'hit-table' ? 'hit' : 'hr';
  applyFilters(tab);
}

// ── Date navigation ───────────────────────────────────────────────────────────
(function () {
  const MIN_DATE = '2025-03-20';

  function _maxDate() {
    const d = new Date(); d.setDate(d.getDate() + 7);
    return d.toISOString().slice(0, 10);
  }

  function navigateToDate(val) {
    if (!val) return;
    const max = _maxDate();
    if (val < MIN_DATE || val > max) return;

    // Show loading state so user knows the click registered
    const prev  = document.getElementById('btn-prev');
    const next  = document.getElementById('btn-next');
    const label = document.getElementById('date-display');
    if (prev)  { prev.disabled = true;  prev.style.opacity = '0.4'; }
    if (next)  { next.disabled = true;  next.style.opacity = '0.4'; }
    if (label) label.textContent = 'Loading…';

    window.location.href = '/date/' + val;
  }

  function shiftDate(days) {
    const nav = document.getElementById('date-nav');
    const current = nav ? nav.dataset.date : new Date().toISOString().slice(0, 10);
    const d = new Date(current + 'T12:00:00');
    d.setDate(d.getDate() + days);
    navigateToDate(d.toISOString().slice(0, 10));
  }

  // Wire up buttons once DOM is ready (script is at bottom of body)
  const btnPrev  = document.getElementById('btn-prev');
  const btnNext  = document.getElementById('btn-next');
  const btnLabel = document.getElementById('btn-date-label');
  const picker   = document.getElementById('date-picker');

  if (btnPrev)  btnPrev.addEventListener('click',  () => shiftDate(-1));
  if (btnNext)  btnNext.addEventListener('click',  () => shiftDate(1));
  if (picker)   picker.addEventListener('change',  (e) => navigateToDate(e.target.value));
  if (btnLabel && picker) {
    btnLabel.addEventListener('click', () => {
      if (picker.showPicker) picker.showPicker(); else picker.click();
    });
  }

  // Expose globally so any inline onclicks still work
  window.navigateToDate = navigateToDate;
  window.shiftDate = shiftDate;
})();

// ── Explain Drawer ────────────────────────────────────────────────────────────
// Slide-in detail panel for hit and HR props.
// Opens on any .btn-explain click via event delegation.
// Fetches /api/prop/{id}/{type}/explain once per player+type, then caches in memory.
(function () {
  const overlay = document.getElementById('explain-overlay');
  const drawer  = document.getElementById('explain-drawer');
  if (!drawer || !overlay) return;

  // In-session cache — keyed "playerId_propType"
  const _cache    = new Map();
  const _inFlight = new Set(); // keys with a fetch currently in flight
  let _activeKey  = null;

  // ── Event delegation for all .btn-explain clicks ──────────────────────────
  document.addEventListener('click', function (e) {
    const btn = e.target.closest('.btn-explain');
    if (!btn) return;
    const playerId = btn.dataset.playerId;
    const propType = btn.dataset.propType;
    if (playerId && propType) _openDrawer(playerId, propType);
  });

  // ── Open ──────────────────────────────────────────────────────────────────
  function _openDrawer(playerId, propType) {
    const date = (document.getElementById('date-nav') || {}).dataset?.date
               || new Date().toISOString().slice(0, 10);

    const key = playerId + '_' + propType;
    _activeKey = key;

    overlay.classList.add('open');
    drawer.classList.add('open');
    drawer.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';

    _resetToSummaryTab();

    if (_cache.has(key)) {
      _renderDrawer(_cache.get(key), propType, playerId);
      return;
    }

    // If a fetch is already running for this key, just show the loading state — don't fire again.
    if (_inFlight.has(key)) {
      _setLoadingState(propType);
      return;
    }

    _inFlight.add(key);
    _setButtonsLoading(playerId, propType, true);
    _setLoadingState(propType);

    fetch('/api/prop/' + playerId + '/' + propType + '/explain?date=' + date)
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (data) {
        _inFlight.delete(key);
        _setButtonsLoading(playerId, propType, false);
        _cache.set(key, data);
        if (_activeKey === key) _renderDrawer(data, propType, playerId);
      })
      .catch(function () {
        _inFlight.delete(key);
        _setButtonsLoading(playerId, propType, false);
        if (_activeKey === key) _renderError();
      });
  }

  // ── Close ─────────────────────────────────────────────────────────────────
  function _closeDrawer() {
    overlay.classList.remove('open');
    drawer.classList.remove('open');
    drawer.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = '';
    _activeKey = null;
  }

  document.getElementById('explain-close').addEventListener('click', _closeDrawer);
  overlay.addEventListener('click', _closeDrawer);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') _closeDrawer();
  });

  // ── Tab switching ─────────────────────────────────────────────────────────
  drawer.querySelectorAll('.ed-tab-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      drawer.querySelectorAll('.ed-tab-btn').forEach(function (b) { b.classList.remove('active'); });
      drawer.querySelectorAll('.ed-pane').forEach(function (p) { p.classList.remove('active'); });
      this.classList.add('active');
      const pane = drawer.querySelector('.ed-pane[data-pane="' + this.dataset.tab + '"]');
      if (pane) pane.classList.add('active');
    });
  });

  function _resetToSummaryTab() {
    drawer.querySelectorAll('.ed-tab-btn').forEach(function (b) { b.classList.remove('active'); });
    drawer.querySelector('.ed-tab-btn[data-tab="summary"]').classList.add('active');
    drawer.querySelectorAll('.ed-pane').forEach(function (p) { p.classList.remove('active'); });
    drawer.querySelector('.ed-pane[data-pane="summary"]').classList.add('active');
    drawer.querySelector('.ed-body').scrollTop = 0;
  }

  // ── Loading / error states ────────────────────────────────────────────────
  const _SVG_PH = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 60 60'%3E%3Ccircle cx='30' cy='30' r='30' fill='%23232830'/%3E%3Ccircle cx='30' cy='23' r='11' fill='%234a5060'/%3E%3Cellipse cx='30' cy='52' rx='19' ry='14' fill='%234a5060'/%3E%3C/svg%3E";

  function _setLoadingState(propType) {
    const hs = drawer.querySelector('.ed-headshot');
    hs.src = _SVG_PH; hs.onerror = null;
    drawer.querySelector('.ed-player-name').textContent = 'Loading…';
    drawer.querySelector('.ed-player-meta').textContent = propType === 'hr' ? 'Home Run' : 'Gets a Hit';
    drawer.querySelector('.ed-prob-pill').textContent = '—';
    _setVerdictPill('—', '');
    drawer.querySelectorAll('.ed-pane').forEach(function (p) {
      p.innerHTML = '<div class="ed-loading"><div class="ed-spinner"></div><span>Loading…</span></div>';
    });
    _resetToSummaryTab();
  }

  function _renderError() {
    drawer.querySelectorAll('.ed-pane').forEach(function (p) {
      p.innerHTML = '<div class="ed-empty">Failed to load data — please try again.</div>';
    });
  }

  function _setVerdictPill(text, cls) {
    const pill = drawer.querySelector('.ed-verdict-pill');
    pill.textContent = text;
    pill.className   = 'ed-verdict-pill ed-verdict-' + (cls || 'no');
  }

  // ── Main render ───────────────────────────────────────────────────────────
  function _renderDrawer(data, propType, playerId) {
    const player  = data.player || {};
    const prop    = data.prop   || {};
    const exp     = data.explanation || {};
    const prob    = prop.model_probability || 0;
    const verdict = prop.verdict || 'NO';
    const pct     = (prob * 100).toFixed(1);

    // Header — headshot
    const hs = drawer.querySelector('.ed-headshot');
    if (playerId && playerId !== '0') {
      hs.src = 'https://img.mlbstatic.com/mlb-photos/image/upload/d_people:generic:headshot:67:current.png/w_213,q_auto:best/v1/people/' + playerId + '/headshot/67/current';
      hs.onerror = function () { hs.src = _SVG_PH; hs.onerror = null; };
    } else {
      hs.src = _SVG_PH;
    }

    // Header — text
    drawer.querySelector('.ed-player-name').textContent = player.name || '—';
    const propLabel = propType === 'hr' ? 'Home Run' : 'Gets a Hit';
    const posLabel  = player.lineup_position ? '#' + player.lineup_position : '';
    const handLabel = player.hand ? 'Bats ' + player.hand : '';
    const metaParts = [player.team, posLabel, handLabel, propLabel].filter(Boolean);
    drawer.querySelector('.ed-player-meta').textContent = metaParts.join(' · ');
    drawer.querySelector('.ed-prob-pill').textContent = pct + '%';
    _setVerdictPill(verdict, verdict.toLowerCase());

    // Render all 5 panes
    _renderSummaryTab(data, propType, prob, pct, exp);
    _renderMatchupTab(data);
    _renderArsenalTab(data);
    _renderWeatherTab(data);
    _renderOddsTab(data, propType, prob);

    _resetToSummaryTab();
  }

  // ── Summary tab ───────────────────────────────────────────────────────────
  function _renderSummaryTab(data, propType, prob, pct, exp) {
    const pane = drawer.querySelector('.ed-pane[data-pane="summary"]');
    const bs   = data.batter_season || {};
    let html   = '';

    // Probability hero
    html += '<div class="ed-prob-hero">';
    html += '<div class="ed-prob-hero-left">';
    html += '<span class="ed-hero-prob-val">' + _esc(pct) + '%</span>';
    html += '<span class="ed-hero-prob-lbl">' + (propType === 'hr' ? 'HR Probability' : 'Hit Probability') + '</span>';
    html += '</div>';
    if (exp.confidence_summary) {
      html += '<div class="ed-confidence-text">' + _esc(exp.confidence_summary) + '</div>';
    }
    html += '</div>';

    // AI explanation
    html += '<h3 class="ed-section-title">AI Analysis</h3>';
    if (exp.available && exp.explanation) {
      html += '<div class="ed-explanation">' + _esc(exp.explanation) + '</div>';
    } else {
      const msg = exp.error ? 'Unavailable: ' + exp.error : 'AI explanation not available for this prop.';
      html += '<div class="ed-explanation-unavailable">' + _esc(msg) + '</div>';
    }

    // Key factors — why this prop is good
    const kf = exp.key_factors || [];
    if (kf.length) {
      html += '<h3 class="ed-section-title">Why This Prop</h3>';
      html += '<div class="ed-factor-list">';
      kf.forEach(function (f) {
        html += '<div class="ed-factor-item"><span class="ed-factor-icon ed-icon-green">&#x2713;</span><span>' + _esc(f) + '</span></div>';
      });
      html += '</div>';
    }

    // Risk factors
    const rf = exp.risk_factors || [];
    if (rf.length) {
      html += '<h3 class="ed-section-title">Risk Factors</h3>';
      html += '<div class="ed-factor-list">';
      rf.forEach(function (f) {
        html += '<div class="ed-factor-item"><span class="ed-factor-icon ed-icon-red">!</span><span>' + _esc(f) + '</span></div>';
      });
      html += '</div>';
    }

    // Sample size warnings
    const sw = exp.sample_size_warnings || [];
    if (sw.length) {
      html += '<h3 class="ed-section-title">Data Caveats</h3>';
      sw.forEach(function (w) {
        html += '<div class="ed-warning-pill"><span>&#x26A0;</span><span>' + _esc(w) + '</span></div>';
      });
    }

    // Key stats (only non-null values)
    html += '<h3 class="ed-section-title">Key Stats</h3><div>';
    if (propType === 'hit') {
      if (bs.xba)            html += _statRow('xBA',            _d3(bs.xba),                          bs.xba >= 0.28 ? 'hi' : '');
      if (bs.xwoba)          html += _statRow('xwOBA',          _d3(bs.xwoba),                         '');
      if (bs.hard_hit_pct)   html += _statRow('Hard Hit%',      _pct(bs.hard_hit_pct),                 bs.hard_hit_pct >= 0.40 ? 'hi' : '');
      if (bs.whiff_pct)      html += _statRow('Whiff%',         _pct(bs.whiff_pct),                    bs.whiff_pct <= 0.18 ? 'lo' : bs.whiff_pct >= 0.30 ? 'bad' : '');
      if (bs.sweet_spot_pct) html += _statRow('Sweet Spot%',    _pct(bs.sweet_spot_pct),               '');
      if (bs.recent_avg)     html += _statRow('Recent AVG (14d)',_d3(bs.recent_avg),                   '');
    } else {
      if (bs.barrel_pct)     html += _statRow('Barrel%',        _pct(bs.barrel_pct),                   bs.barrel_pct >= 0.10 ? 'pow' : '');
      if (bs.exit_velocity)  html += _statRow('Exit Velocity',  _d1(bs.exit_velocity) + ' mph',        bs.exit_velocity >= 92 ? 'hi' : '');
      if (bs.ev50)           html += _statRow('EV50',           _d1(bs.ev50) + ' mph',                 '');
      if (bs.xwoba)          html += _statRow('xwOBA',          _d3(bs.xwoba),                         '');
      if (bs.hr_fb_ratio)    html += _statRow('HR/FB%',         _pct(bs.hr_fb_ratio),                  '');
    }
    html += '</div>';

    pane.innerHTML = html;
  }

  // ── Matchup tab ───────────────────────────────────────────────────────────
  function _renderMatchupTab(data) {
    const pane    = drawer.querySelector('.ed-pane[data-pane="matchup"]');
    const pitcher = data.pitcher || {};
    const bvp     = data.bvp    || {};
    const notes   = data.pitch_matchup_notes || [];
    let html = '';

    // Career BvP
    html += '<h3 class="ed-section-title" style="margin-top:0">Career Batter vs. Pitcher</h3>';
    if (bvp.available && (bvp.ab || 0) >= 3) {
      html += '<div class="ed-bvp-block">';
      html += _bvpStat(bvp.ab   || 0,      'AB');
      html += _bvpStat(bvp.avg  || '.000', 'AVG');
      html += _bvpStat(bvp.hr   || 0,      'HR');
      html += _bvpStat(bvp.k    || 0,      'K');
      html += '</div>';
      const quality = (bvp.ab || 0) >= 50 ? 'large' : (bvp.ab || 0) >= 20 ? 'moderate' : (bvp.ab || 0) >= 10 ? 'small' : 'tiny';
      html += '<div class="ed-bvp-sample">' + (bvp.ab || 0) + ' career AB — ' + quality + ' sample</div>';
      if (bvp.warning) {
        html += '<div class="ed-warning-pill" style="margin-top:0.5rem"><span>&#x26A0;</span><span>' + _esc(bvp.warning) + '</span></div>';
      }
    } else {
      html += '<div class="ed-empty" style="padding:1.25rem 0">No career matchup history (fewer than 3 AB).</div>';
    }

    // Pitcher profile
    html += '<h3 class="ed-section-title">Pitcher Profile</h3><div>';
    if (pitcher.name)                  html += _statRow('Pitcher',           _esc(pitcher.name),                      '');
    if (pitcher.hand)                  html += _statRow('Throws',            pitcher.hand + 'HP',                     '');
    if (pitcher.xera)                  html += _statRow('xERA',              _d2(pitcher.xera),                       pitcher.xera < 3.5 ? 'bad' : pitcher.xera > 4.5 ? 'hi' : '');
    if (pitcher.era)                   html += _statRow('ERA',               _d2(pitcher.era),                        '');
    if (pitcher.k_pct)                 html += _statRow('K%',                _pct(pitcher.k_pct),                     pitcher.k_pct >= 0.28 ? 'bad' : '');
    if (pitcher.bb_pct)                html += _statRow('BB%',               _pct(pitcher.bb_pct),                    '');
    if (pitcher.hard_hit_pct_allowed)  html += _statRow('Hard Hit Allowed%', _pct(pitcher.hard_hit_pct_allowed),      pitcher.hard_hit_pct_allowed >= 0.40 ? 'bad' : '');
    if (pitcher.xwoba_allowed)         html += _statRow('xwOBA Allowed',     _d3(pitcher.xwoba_allowed),              '');
    if (!pitcher.available) html += '<div class="ed-bvp-sample">Full Savant data not available for this pitcher.</div>';
    html += '</div>';

    // Pitch matchup notes
    if (notes.length) {
      html += '<h3 class="ed-section-title">Pitch Matchup Notes</h3>';
      html += '<div class="ed-notes-list">';
      notes.forEach(function (n) {
        html += '<div class="ed-note-item">' + _esc(n) + '</div>';
      });
      html += '</div>';
    }

    pane.innerHTML = html;
  }

  // ── Pitch Arsenal tab ─────────────────────────────────────────────────────
  function _renderArsenalTab(data) {
    const pane    = drawer.querySelector('.ed-pane[data-pane="arsenal"]');
    const arsenal = data.pitch_arsenal    || {};
    const bvpitch = data.batter_vs_pitch  || {};
    const pitches = arsenal.pitches       || [];
    const splits  = bvpitch.splits        || [];

    if (!arsenal.available || !pitches.length) {
      pane.innerHTML = '<div class="ed-empty">No pitch arsenal data available for this pitcher.</div>';
      return;
    }

    let html = '<h3 class="ed-section-title" style="margin-top:0">Pitcher Pitch Mix</h3><div>';
    pitches.forEach(function (p) {
      const usagePct = Math.round(p.usage_pct || 0);
      html += '<div class="ed-pitch-row">';
      html += '<div class="ed-pitch-name">' + _esc(p.label || p.pitch_type || '—') + '</div>';
      html += '<div class="ed-pitch-bar-wrap"><div class="ed-pitch-bar-bg"><div class="ed-pitch-bar-fill" style="width:' + Math.min(usagePct, 100) + '%"></div></div></div>';
      html += '<div class="ed-pitch-stats">';
      html += _pitchStat(usagePct + '%',                       'USE');
      if (p.whiff_pct   != null) html += _pitchStat(p.whiff_pct + '%',   'WHIFF');
      if (p.avg_velocity)        html += _pitchStat(p.avg_velocity,       'MPH');
      html += '</div></div>';
    });
    html += '</div>';

    // Batter vs pitch type
    const realSplits = splits.filter(function (s) { return s.pa && s.pa >= 5; });
    if (bvpitch.available && realSplits.length) {
      html += '<h3 class="ed-section-title">Batter vs. Pitch Type</h3><div>';
      realSplits.forEach(function (s) {
        html += '<div class="ed-pitch-row">';
        html += '<div class="ed-pitch-name">' + _esc(s.label || s.pitch_type || '—') + '</div>';
        html += '<div class="ed-pitch-bar-wrap"></div>';
        html += '<div class="ed-pitch-stats">';
        if (s.ba    != null) html += _pitchStat(_d3(s.ba),          'AVG');
        if (s.whiff_pct != null) html += _pitchStat(s.whiff_pct + '%', 'WHIFF');
        if (s.pa)            html += _pitchStat(s.pa,               'PA');
        html += '</div></div>';
      });
      html += '</div>';
    }

    pane.innerHTML = html;
  }

  // ── Weather & Park tab ────────────────────────────────────────────────────
  function _renderWeatherTab(data) {
    const pane    = drawer.querySelector('.ed-pane[data-pane="weather"]');
    const ctx     = data.context || {};
    const weather = ctx.weather  || {};
    const park    = ctx.park     || {};
    let html = '';

    // Stadium / park factors
    html += '<h3 class="ed-section-title" style="margin-top:0">Stadium</h3>';
    const parkName = park.name || ctx.venue || '—';
    const hitF     = park.hit_factor;
    const hrF      = park.hr_factor;
    const hitCls   = hitF >= 105 ? 'hi' : hitF <= 95 ? 'lo' : '';
    html += '<div class="ed-park-block">';
    html += '<div>';
    html += '<div class="ed-park-name">' + _esc(parkName) + '</div>';
    if (park.available && hitF) {
      html += '<div class="ed-park-sub">Hit factor: ' + Math.round(hitF) + ' · HR factor: ' + (hrF ? Math.round(hrF) : '—') + ' &nbsp;(100&nbsp;=&nbsp;avg)</div>';
    } else {
      html += '<div class="ed-park-sub">Park factor data unavailable</div>';
    }
    html += '</div>';
    if (park.available && hitF) {
      html += '<div class="ed-park-factor ' + hitCls + '">' + Math.round(hitF) + '</div>';
    }
    html += '</div>';

    // Weather conditions
    html += '<h3 class="ed-section-title">Conditions</h3>';
    if (weather.is_dome) {
      html += '<div style="padding:0.6rem 0;color:var(--muted);font-size:0.875rem;">&#x1F3DF; Indoor dome — weather conditions do not apply.</div>';
    } else if (weather.temp_f != null) {
      html += '<div class="ed-weather-grid">';
      html += _wxCard(Math.round(weather.temp_f) + '°F',                                           'Temp');
      html += _wxCard((weather.wind_speed_mph ? Math.round(weather.wind_speed_mph) : '—') + ' mph','Wind');
      html += _wxCard(weather.wind_direction_label || '—',                                          'Direction');
      html += '</div>';
      if (weather.condition) {
        html += '<div class="ed-stat-row"><span class="ed-stat-label">Sky</span><span class="ed-stat-val">' + _esc(weather.condition) + '</span></div>';
      }
      // Impact chips
      const chips = [];
      const wSpd  = weather.wind_speed_mph || 0;
      const wDir  = (weather.wind_direction_label || '').toLowerCase();
      if (weather.temp_f < 50) chips.push({ t: '&#x2744; Cold — suppresses contact &amp; power', c: 'chip-suppress' });
      if (weather.temp_f > 80) chips.push({ t: '&#x2600; Warm — favors offense', c: 'chip-boost' });
      if (wSpd >= 8  && wDir.includes('out')) chips.push({ t: '&#x1F4A8; Wind blowing out — HR boost', c: 'chip-boost' });
      if (wSpd >= 12 && wDir.includes('in'))  chips.push({ t: '&#x1F4A8; Wind blowing in — suppresses HR', c: 'chip-suppress' });
      if (!chips.length) chips.push({ t: 'Neutral conditions', c: 'chip-neutral' });
      html += '<div class="ed-impact-chips">';
      chips.forEach(function (c) { html += '<span class="ed-impact-chip ' + c.c + '">' + c.t + '</span>'; });
      html += '</div>';
    } else {
      html += '<div class="ed-empty" style="padding:1rem 0">Weather data unavailable.</div>';
    }

    pane.innerHTML = html;
  }

  // ── Odds & Edge tab ───────────────────────────────────────────────────────
  function _renderOddsTab(data, propType, modelProb) {
    const pane = drawer.querySelector('.ed-pane[data-pane="odds"]');
    const prop = data.prop || {};
    const odds = prop.odds || {};
    let html = '';

    if (odds.available) {
      // Model vs market summary
      html += '<h3 class="ed-section-title" style="margin-top:0">Model vs. Market</h3><div>';
      html += _statRow('Model Probability', (modelProb * 100).toFixed(1) + '%', '');
      if (odds.implied_probability != null) {
        html += _statRow('Market Implied Prob', (odds.implied_probability * 100).toFixed(1) + '%', '');
      }
      if (odds.edge != null) {
        const edgePct = (odds.edge * 100).toFixed(1);
        const eCls    = odds.edge > 0 ? 'hi' : 'bad';
        html += _statRow('Edge vs. Market', (odds.edge > 0 ? '+' : '') + edgePct + 'pp', eCls);
      }
      html += '</div>';

      // Best available line
      if (odds.sportsbook_line) {
        html += '<h3 class="ed-section-title">Best Line</h3>';
        html += '<div class="ed-odds-row best-odds">';
        html += '<span class="ed-odds-book">' + _esc(odds.best_book || 'Best') + ' &#x2605;</span>';
        html += '<span class="ed-odds-line">'  + _esc(String(odds.sportsbook_line)) + '</span>';
        if (odds.implied_probability != null) {
          html += '<span class="ed-odds-implied">Impl. ' + (odds.implied_probability * 100).toFixed(1) + '%</span>';
        }
        if (odds.edge != null) {
          const eCls = odds.edge > 0 ? 'ed-edge-pos' : 'ed-edge-neg';
          html += '<span class="ed-edge-chip ' + eCls + '">' + (odds.edge > 0 ? '+' : '') + (odds.edge * 100).toFixed(1) + 'pp</span>';
        }
        html += '</div>';
      }

      // Full sportsbook breakdown
      const sb = odds.sportsbook_odds;
      if (sb && typeof sb === 'object' && Object.keys(sb).length) {
        html += '<h3 class="ed-section-title">All Sportsbooks</h3>';
        html += '<div class="ed-odds-list">';
        Object.entries(sb).forEach(function (_ref) {
          const book = _ref[0], line = _ref[1];
          const isBest = book === odds.best_book;
          const imp    = _impliedProb(line);
          html += '<div class="ed-odds-row' + (isBest ? ' best-odds' : '') + '">';
          html += '<span class="ed-odds-book">' + _esc(book) + (isBest ? ' &#x2605;' : '') + '</span>';
          html += '<span class="ed-odds-line">' + _esc(String(line)) + '</span>';
          html += '<span class="ed-odds-implied">' + (imp != null ? (imp * 100).toFixed(1) + '%' : '—') + '</span>';
          if (imp != null) {
            const ev   = modelProb - imp;
            const eCls = ev > 0 ? 'ed-edge-pos' : 'ed-edge-neg';
            html += '<span class="ed-edge-chip ' + eCls + '">' + (ev > 0 ? '+' : '') + (ev * 100).toFixed(1) + 'pp</span>';
          }
          html += '</div>';
        });
        html += '</div>';
      }
    } else {
      html += '<div class="ed-no-odds">';
      html += '<div style="font-size:1.5rem;margin-bottom:0.5rem">&#x1F4CA;</div>';
      html += '<strong>No odds data</strong><br><br>';
      html += '<span>Use &#x21BB; Refresh Odds on the Best Bets tab to load live sportsbook lines.</span>';
      html += '</div>';
    }

    pane.innerHTML = html;
  }

  // ── Helpers ───────────────────────────────────────────────────────────────
  function _setButtonsLoading(playerId, propType, loading) {
    document.querySelectorAll(
      '.btn-explain[data-player-id="' + playerId + '"][data-prop-type="' + propType + '"]'
    ).forEach(function (b) {
      b.disabled = loading;
      b.classList.toggle('btn-explain--loading', loading);
    });
  }

  function _esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function _d1(v) { return v == null ? '—' : (+v).toFixed(1); }
  function _d2(v) { return v == null ? '—' : (+v).toFixed(2); }
  function _d3(v) { return v == null ? '—' : (+v).toFixed(3); }
  function _pct(v) { return v == null ? '—' : (v * 100).toFixed(1) + '%'; }

  function _statRow(lbl, val, cls) {
    return '<div class="ed-stat-row"><span class="ed-stat-label">' + _esc(lbl) + '</span>'
         + '<span class="ed-stat-val' + (cls ? ' ' + cls : '') + '">' + _esc(String(val)) + '</span></div>';
  }
  function _bvpStat(val, lbl) {
    return '<div class="ed-bvp-stat"><div class="ed-bvp-val">' + _esc(String(val)) + '</div>'
         + '<div class="ed-bvp-lbl">' + _esc(lbl) + '</div></div>';
  }
  function _pitchStat(val, lbl) {
    return '<div class="ed-pitch-stat"><div class="ed-pitch-stat-val">' + _esc(String(val)) + '</div>'
         + '<div class="ed-pitch-stat-lbl">' + _esc(lbl) + '</div></div>';
  }
  function _wxCard(val, lbl) {
    return '<div class="ed-weather-card"><span class="ed-weather-val">' + _esc(String(val)) + '</span>'
         + '<span class="ed-weather-lbl">' + _esc(lbl) + '</span></div>';
  }
  function _impliedProb(americanOdds) {
    const o = parseInt(String(americanOdds).replace(/[^0-9\-]/g, ''), 10);
    if (isNaN(o) || o === 0) return null;
    return o > 0 ? 100 / (o + 100) : Math.abs(o) / (Math.abs(o) + 100);
  }
})();
