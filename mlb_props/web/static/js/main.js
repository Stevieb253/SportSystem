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

  // Update header indicator
  table.querySelectorAll('th').forEach((th, i) => {
    th.textContent = th.textContent.replace(/ [▲▼]$/, '');
    if (i === colIndex) th.textContent += asc ? ' ▲' : ' ▼';
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
