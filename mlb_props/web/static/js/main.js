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
// matchup: null | { away: 'ATL', home: 'CIN' }  — shared across both tabs.
const _filterState = {
  hit: { verdict: '', team: '', hand: '', slot: '', matchup: null },
  hr:  { verdict: '', team: '', hand: '', slot: '', matchup: null },
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

// Called by the team <select> onchange — clears matchup when a specific team is chosen
function onTeamDropdownChange(tab) {
  var teamEl = document.getElementById(tab + '-team');
  if (teamEl && teamEl.value !== '') {
    // Selecting a specific team clears the shared matchup filter on both tabs
    _filterState.hit.matchup = null;
    _filterState.hr.matchup  = null;
    _setActiveMatchupPill('hit', null);
    _setActiveMatchupPill('hr',  null);
  }
  applyFilters(tab);
}

function applyFilters(tab) {
  var state   = _filterState[tab];
  var search  = (document.getElementById(tab + '-search')?.value || '').toLowerCase();
  // When a matchup is active ignore the team dropdown — matchup takes priority
  var teamSel = state.matchup ? '' : (document.getElementById(tab + '-team')?.value || '');

  if (tab === 'hit') {
    var table = document.getElementById('hit-table');
    if (!table) return;
    table.querySelectorAll('tbody tr').forEach(function(row) {
      row.style.display = _rowMatches(row, state, search, teamSel, 'table') ? '' : 'none';
    });
  } else {
    var grid = document.getElementById('hr-grid');
    if (!grid) return;
    grid.querySelectorAll('.hr-card').forEach(function(card) {
      card.style.display = _rowMatches(card, state, search, teamSel, 'card') ? '' : 'none';
    });
  }
}

function _rowMatches(el, state, search, teamSel, type) {
  // Verdict
  if (state.verdict && el.dataset.verdict !== state.verdict) return false;

  // Matchup filter: both teams in the game must match
  if (state.matchup) {
    var gAway = el.dataset.gameAway || '';
    var gHome = el.dataset.gameHome || '';
    if (gAway !== state.matchup.away || gHome !== state.matchup.home) return false;
  } else if (teamSel) {
    // Fall back to single-team dropdown
    if (el.dataset.team !== teamSel) return false;
  }

  // Hand
  if (state.hand && el.dataset.hand !== state.hand) return false;

  // Lineup slot
  if (state.slot) {
    var pos = parseInt(el.dataset.pos || '0', 10);
    if (state.slot === 'top' && (pos < 1 || pos > 3)) return false;
    if (state.slot === 'mid' && (pos < 4 || pos > 6)) return false;
    if (state.slot === 'bot' && (pos < 7 || pos > 9)) return false;
  }

  // Text search (player name)
  if (search) {
    var nameEl = type === 'table'
      ? el.cells[0]?.textContent
      : el.querySelector('.hr-player-name')?.textContent;
    if (!(nameEl || '').toLowerCase().includes(search)) return false;
  }

  return true;
}

// ── Matchup filter pills ──────────────────────────────────────────────────────
// Reads window.MLB_GAMES (injected by the template) and builds a scrollable
// pill row for each prop tab.  Matchup state is shared across hit + hr tabs.

function selectMatchupPill(btn, tab, matchup) {
  // Sync matchup across both tabs so switching tabs preserves the selection
  _filterState.hit.matchup = matchup;
  _filterState.hr.matchup  = matchup;

  // If a matchup is selected, reset team dropdowns to "All Teams"
  if (matchup) {
    ['hit', 'hr'].forEach(function(t) {
      var el = document.getElementById(t + '-team');
      if (el) el.value = '';
    });
  }

  // Highlight the pill in both bars so whichever tab you're on looks correct
  _setActiveMatchupPill('hit', matchup);
  _setActiveMatchupPill('hr',  matchup);

  // Re-filter the tab that was clicked
  applyFilters(tab);
}

function _setActiveMatchupPill(tab, matchup) {
  var bar = document.getElementById(tab + '-matchup-bar');
  if (!bar) return;
  bar.querySelectorAll('.mf-pill').forEach(function(p) {
    p.classList.remove('mf-pill-active');
  });
  if (!matchup) {
    var allPill = bar.querySelector('.mf-pill-all');
    if (allPill) allPill.classList.add('mf-pill-active');
  } else {
    bar.querySelectorAll('.mf-pill').forEach(function(p) {
      if (p.dataset.away === matchup.away && p.dataset.home === matchup.home) {
        p.classList.add('mf-pill-active');
      }
    });
  }
}

function _buildMatchupPills(tab) {
  var bar = document.getElementById(tab + '-matchup-bar');
  console.log('[matchup] _buildMatchupPills("' + tab + '") — bar:', bar,
              '| MLB_GAMES length:', (window.MLB_GAMES || []).length);

  if (!bar) {
    console.warn('[matchup] bar element not found for tab:', tab);
    return;
  }

  var games = (window.MLB_GAMES || []).filter(function(g) {
    return g.away_abbr && g.home_abbr;
  });

  console.log('[matchup] games after filter:', games.length, '| raw MLB_GAMES:', (window.MLB_GAMES || []).length);

  // Hide the bar entirely if there are no games (e.g. lineup data not yet loaded)
  if (!games.length) {
    console.warn('[matchup] No valid games — hiding bar for tab:', tab);
    bar.style.display = 'none';
    return;
  }

  // Sort earliest game first
  games.sort(function(a, b) {
    var ta = a.game_time || '';
    var tb = b.game_time || '';
    return ta < tb ? -1 : ta > tb ? 1 : 0;
  });

  var html = '<span class="mf-label">Filter by matchup</span><div class="mf-pills">';

  // "All Games" pill — active by default
  html += '<button class="mf-pill mf-pill-all mf-pill-active"'
        + ' data-away="" data-home=""'
        + ' onclick="selectMatchupPill(this,\'' + tab + '\',null)">All Games</button>';

  games.forEach(function(g) {
    var away     = g.away_abbr;
    var home     = g.home_abbr;
    var awayLogo = g.away_logo || '';
    var homeLogo = g.home_logo || '';

    // Parse local game time → "7:10 PM"
    var timeStr = '';
    if (g.game_time) {
      try {
        var d = new Date(g.game_time);
        if (!isNaN(d.getTime())) {
          timeStr = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
        }
      } catch (_) {}
    }

    // Encode matchup object for inline onclick (avoids quoting issues)
    var matchupJson = '{away:\'' + away + '\',home:\'' + home + '\'}';

    html += '<button class="mf-pill"'
          + ' data-away="' + away + '" data-home="' + home + '"'
          + ' onclick="selectMatchupPill(this,\'' + tab + '\',' + matchupJson + ')">';
    if (awayLogo) {
      html += '<img class="mf-logo" src="' + awayLogo + '" alt="' + away + '"'
            + ' onerror="this.style.display=\'none\'">';
    }
    html += '<span class="mf-abbr">' + away + '</span>';
    html += '<span class="mf-sep">@</span>';
    if (homeLogo) {
      html += '<img class="mf-logo" src="' + homeLogo + '" alt="' + home + '"'
            + ' onerror="this.style.display=\'none\'">';
    }
    html += '<span class="mf-abbr">' + home + '</span>';
    if (timeStr) html += '<span class="mf-time">' + timeStr + '</span>';
    html += '</button>';
  });

  html += '</div>';
  bar.innerHTML = html;

  var pillCount = bar.querySelectorAll('.mf-pill').length;
  console.log('[matchup] Pills rendered for "' + tab + '":', pillCount,
              '(including All Games pill)');
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

// NOTE: _buildMatchupPills() is called from index.html's extra_js block
// AFTER window.MLB_GAMES is defined. Calling it here would be too early
// because main.js loads before the extra_js block runs.

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
    _renderWeatherTab(data, propType);
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

    // AI explanation — always render a consistent card
    const cleanText = _cleanExplanationText(exp.available ? exp.explanation : null);
    html += '<div class="ed-ai-card">';
    html += '<div class="ed-ai-card-header"><span class="ed-ai-icon">&#x2736;</span><span>Matchup Analysis</span></div>';
    if (cleanText) {
      html += '<p class="ed-explanation">' + _esc(cleanText) + '</p>';
    } else {
      html += '<p class="ed-explanation-unavailable">Analysis is temporarily unavailable. The matchup data below is still current.</p>';
    }
    html += '</div>';

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
  function _renderWeatherTab(data, propType) {
    var pane    = drawer.querySelector('.ed-pane[data-pane="weather"]');
    var ctx     = data.context || {};
    var weather = ctx.weather  || {};
    var park    = ctx.park     || {};
    var isHR    = propType === 'hr';
    var html    = '';

    // ── SECTION 1: Stadium ──────────────────────────────────────────────────
    var parkName = park.name || ctx.venue || '—';
    var profile  = park.park_profile || 'neutral';
    var profBadgeCls = profile === 'hitter-friendly' ? 'wp-badge-hitter'
                     : profile === 'pitcher-friendly' ? 'wp-badge-pitcher'
                     : 'wp-badge-neutral';
    var profLabel    = profile === 'hitter-friendly' ? '🏟 Hitter Friendly'
                     : profile === 'pitcher-friendly' ? '🏟 Pitcher Friendly'
                     : '🏟 Neutral Park';

    html += '<div class="wp-section wp-section-stadium">';
    html += '<div class="wp-stadium-name">' + _esc(parkName) + '</div>';
    html += '<div class="wp-badges">';
    html += '<span class="wp-badge ' + profBadgeCls + '">' + profLabel + '</span>';
    if (weather.is_dome) {
      html += '<span class="wp-badge wp-badge-dome">🏗 Dome</span>';
    } else if (weather.is_retractable) {
      html += '<span class="wp-badge wp-badge-retractable">↕ Retractable Roof</span>';
    } else {
      html += '<span class="wp-badge wp-badge-outdoor">☀ Outdoor</span>';
    }
    html += '</div></div>'; // badges, section

    // ── SECTION 2: Park Factors ─────────────────────────────────────────────
    html += '<div class="wp-section">';
    html += '<h3 class="wp-section-title">Park Factors <span class="wp-section-avg">100 = League Average</span></h3>';

    if (park.available && park.source !== 'neutral_fallback') {
      html += '<div class="wp-factors-grid">';
      var runF  = park.run_factor;
      var hitF  = park.hit_factor;
      var hrF   = park.hr_factor;
      var lhbHR = park.lhb_hr_factor;
      var rhbHR = park.rhb_hr_factor;
      if (runF) html += _wpFactorCard('Run Factor', runF, '');
      if (hitF) html += _wpFactorCard('Hit Factor', hitF, isHR ? '' : 'primary');
      if (hrF)  html += _wpFactorCard('HR Factor',  hrF,  isHR ? 'primary' : '');
      if (isHR && lhbHR) html += _wpFactorCard('LHB HR Factor', lhbHR, '');
      if (isHR && rhbHR) html += _wpFactorCard('RHB HR Factor', rhbHR, '');
      html += '</div>';
    } else if (park.fallback_note) {
      html += '<div class="wp-fallback-note">' + _esc(park.fallback_note) + '</div>';
    } else {
      html += '<div class="wp-fallback-note">Park factor data unavailable for this stadium.</div>';
    }
    html += '</div>'; // section

    // ── SECTION 3: Conditions ────────────────────────────────────────────────
    html += '<div class="wp-section">';
    html += '<h3 class="wp-section-title">Conditions</h3>';
    if (weather.is_dome) {
      html += '<div class="wp-dome-note">🏗 Indoor dome — weather conditions do not affect play. '
            + 'Park dimensions and run/HR factors still apply.</div>';
    } else if (weather.is_retractable && weather.temp_f == null) {
      html += '<div class="wp-dome-note">↕ Retractable-roof stadium — roof status determines weather impact. '
            + 'Park factors apply regardless of roof position.</div>';
    } else if (weather.temp_f != null) {
      if (weather.is_retractable) {
        html += '<div class="wp-dome-note" style="margin-bottom:0.6rem">↕ Retractable roof — park factors apply regardless of roof position.</div>';
      }
      html += _wpConditionsGrid(weather);
    } else {
      html += '<div class="wp-empty">Weather data unavailable.</div>';
    }
    html += '</div>'; // section

    // ── SECTION 4: Impact Summary ────────────────────────────────────────────
    html += '<div class="wp-section">';
    html += '<h3 class="wp-section-title">Impact Summary</h3>';
    html += '<div class="wp-impact-chips">';
    var chips = _buildImpactChips(weather, park, isHR);
    if (!chips.length) chips = [{ text: '✓ Neutral conditions — no significant environment edge', cls: 'wp-chip-neutral' }];
    chips.forEach(function(c) { html += '<span class="wp-chip ' + c.cls + '">' + c.text + '</span>'; });
    html += '</div></div>'; // chips, section

    // ── SECTION 5: Environment Conclusion ───────────────────────────────────
    var conclusion = _buildEnvConclusion(weather, park, isHR);
    if (conclusion) {
      html += '<div class="wp-section">';
      html += '<h3 class="wp-section-title">Environment Impact</h3>';
      html += '<div class="wp-conclusion ' + conclusion.cls + '">';
      html += '<div class="wp-conclusion-headline">' + _esc(conclusion.headline) + '</div>';
      html += '<div class="wp-conclusion-body">' + _esc(conclusion.body) + '</div>';
      html += '</div></div>';
    }

    // ── SECTION 6: Park Notes ────────────────────────────────────────────────
    var notes = park.tendency_notes;
    if (notes) {
      html += '<div class="wp-section">';
      html += '<h3 class="wp-section-title">Park Notes</h3>';
      html += '<div class="wp-park-notes">' + _esc(notes) + '</div>';
      html += '</div>';
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
  // Sanitise Gemini explanation text before displaying.
  // Handles: raw JSON strings, markdown fences, and empty/null values.
  // Returns a clean plain-text string or null (caller shows fallback message).
  function _cleanExplanationText(raw) {
    if (!raw) return null;
    var s = String(raw).trim();

    // Helper to pull explanation out of a parsed JSON object
    function _fromObj(obj) {
      return (obj && typeof obj.explanation === 'string' && obj.explanation.trim())
        ? obj.explanation.trim() : null;
    }

    // Strip markdown fences (``` or ```json at start, ``` at end)
    var stripped = s.replace(/^```[a-zA-Z]*\s*/i, '').replace(/\s*```\s*$/, '').trim();

    // If it looks like a JSON object after stripping, try to parse it
    if (stripped.charAt(0) === '{') {
      try {
        return _fromObj(JSON.parse(stripped)) || null;
      } catch (e) {
        // Regex fallback — extract "explanation": "..." value
        var m = stripped.match(/"explanation"\s*:\s*"((?:[^"\\]|\\.)*)"/);
        if (m) return m[1].replace(/\\n/g, '\n').replace(/\\"/g, '"').trim() || null;
        // Still looks like JSON but we can't extract anything useful — suppress it
        console.warn('[explain] Could not parse Gemini JSON response:', stripped.slice(0, 200));
        return null;
      }
    }

    // Plain text — return as-is (the server already parsed correctly)
    return stripped || null;
  }

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
  // ── Weather & Park helpers ────────────────────────────────────────────────
  function _wpFactorCard(label, value, highlight) {
    var rounded = Math.round(value);
    var cls = '';
    if (value >= 108) cls = 'wp-factor-hi';
    else if (value <= 93) cls = 'wp-factor-lo';
    var hCls = highlight === 'primary' ? ' wp-factor-primary' : '';
    return '<div class="wp-factor-card' + hCls + '">'
         + '<div class="wp-factor-label">' + _esc(label) + '</div>'
         + '<div class="wp-factor-val ' + cls + '">' + rounded + '</div>'
         + '<div class="wp-factor-desc">' + _esc(_wpFactorDesc(label, value)) + '</div>'
         + '</div>';
  }

  function _wpFactorDesc(label, value) {
    var isHRLabel = label.indexOf('HR') >= 0 || label.indexOf('Run') >= 0;
    if (isHRLabel) {
      if (value >= 120) return 'Home runs are much easier to hit here than anywhere else';
      if (value >= 110) return 'Home runs are easier to hit here than at most stadiums';
      if (value >= 104) return 'Slightly easier to hit home runs here than average';
      if (value >= 97)  return 'About the same as a typical MLB stadium';
      if (value >= 90)  return 'Home runs are harder to hit here than average';
      return 'One of the toughest parks for home runs in the league';
    } else {
      if (value >= 112) return 'Getting a hit is much easier here than at most stadiums';
      if (value >= 106) return 'Getting a hit is easier here than at most stadiums';
      if (value >= 103) return 'Slightly more hits than at a typical stadium';
      if (value >= 97)  return 'About the same hit rate as a typical MLB stadium';
      if (value >= 90)  return 'Getting hits is harder here than average';
      return 'One of the toughest parks for getting hits in the league';
    }
  }

  function _wpConditionsGrid(weather) {
    var html = '<div class="wp-conditions-grid">';
    if (weather.temp_f != null)        html += _wpCondCard(Math.round(weather.temp_f) + '°F', 'Temp', _wpTempClass(weather.temp_f));
    if (weather.wind_speed_mph != null) html += _wpCondCard(Math.round(weather.wind_speed_mph) + ' mph', 'Wind', '');
    if (weather.wind_direction_label)   html += _wpCondCard(weather.wind_direction_label, 'Direction', '');
    if (weather.condition)              html += _wpCondCard(weather.condition, 'Sky', '');
    html += '</div>';
    return html;
  }

  function _wpCondCard(val, lbl, cls) {
    return '<div class="wp-cond-card' + (cls ? ' ' + cls : '') + '">'
         + '<span class="wp-cond-val">' + _esc(String(val)) + '</span>'
         + '<span class="wp-cond-lbl">' + _esc(lbl) + '</span>'
         + '</div>';
  }

  function _wpTempClass(t) {
    if (t < 45) return 'wp-cond-cold';
    if (t > 82) return 'wp-cond-warm';
    return '';
  }

  function _buildImpactChips(weather, park, isHR) {
    var chips = [];
    var wSpd  = weather.wind_speed_mph || 0;
    var wDir  = (weather.wind_direction_label || '').toLowerCase();
    var temp  = weather.temp_f;
    var isDome = weather.is_dome;
    var runF  = park.run_factor  || 100;
    var hrF   = park.hr_factor   || 100;
    var hitF  = park.hit_factor  || 100;

    if (isDome) {
      chips.push({ text: '🏗 Dome neutralizes weather effects', cls: 'wp-chip-neutral' });
    } else {
      if (temp != null && temp < 45)  chips.push({ text: '❄ Cold — suppresses contact & power', cls: 'wp-chip-suppress' });
      if (temp != null && temp > 82)  chips.push({ text: '☀ Warm — favors offense', cls: 'wp-chip-boost' });
      if (wSpd >= 8  && wDir.indexOf('out') >= 0) chips.push({ text: '💨 Wind blowing out — HR boost', cls: 'wp-chip-boost' });
      if (wSpd >= 12 && wDir.indexOf('in')  >= 0) chips.push({ text: '💨 Wind blowing in — suppresses HR', cls: 'wp-chip-suppress' });
      if (wSpd >= 5  && wDir.indexOf('cross') >= 0) chips.push({ text: '💨 Crosswind — unpredictable carry', cls: 'wp-chip-neutral' });
    }

    // Park-based chips
    if (park.available && park.source !== 'neutral_fallback') {
      var profile = park.park_profile || 'neutral';
      if (profile === 'hitter-friendly')  chips.push({ text: '🏟 Hitter-friendly stadium', cls: 'wp-chip-boost' });
      if (profile === 'pitcher-friendly') chips.push({ text: '🏟 Pitcher-friendly stadium', cls: 'wp-chip-suppress' });

      if (isHR) {
        if (hrF >= 110) chips.push({ text: '💣 Strong HR park', cls: 'wp-chip-boost' });
        else if (hrF <= 91) chips.push({ text: '💣 Suppresses home runs', cls: 'wp-chip-suppress' });
      } else {
        if (hitF >= 106) chips.push({ text: '🎯 Favors contact hitters', cls: 'wp-chip-boost' });
        else if (hitF <= 95) chips.push({ text: '🎯 Suppresses hit rate', cls: 'wp-chip-suppress' });
      }
    }

    return chips;
  }

  // Builds a plain-English "Environment Impact" conclusion.
  // Returns { headline, body, cls } or null.
  function _buildEnvConclusion(weather, park, isHR) {
    var isDome = weather.is_dome;
    var temp   = weather.temp_f;
    var wSpd   = weather.wind_speed_mph || 0;
    var wDir   = (weather.wind_direction_label || '').toLowerCase();
    var runF   = park.run_factor  || 100;
    var hrF    = park.hr_factor   || 100;
    var hitF   = park.hit_factor  || 100;
    var src    = park.source || '';

    if (src === 'neutral_fallback' && !temp) return null; // nothing useful to say

    // Score the overall environment: positive = helps hitters, negative = helps pitchers
    var score = 0;
    var reasons = [];

    if (isHR) {
      // Score for HR prop
      if (hrF >= 115)      { score += 3; reasons.push('strong HR park'); }
      else if (hrF >= 108) { score += 2; reasons.push('above-average HR park'); }
      else if (hrF >= 103) { score += 1; reasons.push('slightly above-average HR park'); }
      else if (hrF <= 88)  { score -= 3; reasons.push('one of the toughest HR parks in baseball'); }
      else if (hrF <= 94)  { score -= 2; reasons.push('below-average HR park'); }
      else if (hrF <= 97)  { score -= 1; reasons.push('slightly below-average HR park'); }

      if (!isDome) {
        if (wSpd >= 10 && wDir.indexOf('out') >= 0) { score += 2; reasons.push('wind blowing out'); }
        if (wSpd >= 15 && wDir.indexOf('in')  >= 0) { score -= 2; reasons.push('wind blowing in hard'); }
        if (temp != null && temp > 82) { score += 1; reasons.push('warm weather'); }
        if (temp != null && temp < 45) { score -= 1; reasons.push('cold weather'); }
      }
    } else {
      // Score for hit prop
      if (hitF >= 112)     { score += 3; reasons.push('one of the best parks for getting hits'); }
      else if (hitF >= 106){ score += 2; reasons.push('above-average park for hits'); }
      else if (hitF >= 103){ score += 1; reasons.push('slightly above-average park for hits'); }
      else if (hitF <= 90) { score -= 3; reasons.push('one of the toughest parks for getting hits'); }
      else if (hitF <= 95) { score -= 2; reasons.push('below-average park for hits'); }
      else if (hitF <= 97) { score -= 1; reasons.push('slightly below-average park for hits'); }

      if (!isDome) {
        if (temp != null && temp > 82) { score += 1; reasons.push('warm weather helps offense'); }
        if (temp != null && temp < 45) { score -= 1; reasons.push('cold weather suppresses offense'); }
        if (wSpd >= 15 && wDir.indexOf('in') >= 0) { score -= 1; reasons.push('strong wind blowing in'); }
      }
    }

    var headline, body, cls;

    if (score >= 3) {
      headline = isHR ? 'Strong home run environment.' : 'Very favorable for getting hits.';
      cls = 'wp-conclusion-positive';
      body = isHR
        ? 'The stadium and current conditions combine to create one of the better environments for hitting home runs. '
          + (reasons.length ? 'Key factors: ' + reasons.join(', ') + '.' : '')
        : 'The stadium and conditions make getting a hit easier than at a typical game. '
          + (reasons.length ? 'Key factors: ' + reasons.join(', ') + '.' : '');
    } else if (score >= 1) {
      headline = isHR ? 'Slightly positive for home runs.' : 'Slightly favorable for hits.';
      cls = 'wp-conclusion-slight-positive';
      body = isHR
        ? 'This environment gives hitters a modest boost for home runs compared to the league average.'
        : 'Conditions here give hitters a small advantage over a typical game environment.';
    } else if (score <= -3) {
      headline = isHR ? 'Tough environment for home runs.' : 'Tough environment for getting hits.';
      cls = 'wp-conclusion-negative';
      body = isHR
        ? 'The stadium and conditions make hitting home runs significantly harder than average. '
          + (reasons.length ? 'Key factors: ' + reasons.join(', ') + '.' : '')
        : 'Getting hits here is meaningfully harder than at a typical stadium. '
          + (reasons.length ? 'Key factors: ' + reasons.join(', ') + '.' : '');
    } else if (score <= -1) {
      headline = isHR ? 'Slightly negative for home runs.' : 'Slightly negative for hits.';
      cls = 'wp-conclusion-slight-negative';
      body = isHR
        ? 'This environment gives pitchers a modest edge — home runs are a bit harder to hit than average here.'
        : 'Conditions here give pitchers a small advantage over a typical game environment.';
    } else {
      headline = isHR ? 'Neutral environment for home runs.' : 'Neutral environment for hits.';
      cls = 'wp-conclusion-neutral';
      body = isDome
        ? 'The enclosed dome removes weather as a factor. The park itself plays close to the MLB average.'
        : 'The stadium and current weather conditions are close to the league average — no significant advantage either way.';
    }

    return { headline: headline, body: body, cls: cls };
  }

  function _impliedProb(americanOdds) {
    const o = parseInt(String(americanOdds).replace(/[^0-9\-]/g, ''), 10);
    if (isNaN(o) || o === 0) return null;
    return o > 0 ? 100 / (o + 100) : Math.abs(o) / (Math.abs(o) + 100);
  }
})();
