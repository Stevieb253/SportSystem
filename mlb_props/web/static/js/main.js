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
  const rows  = Array.from(tbody.querySelectorAll('tr:not(.hidden-row)'));
  const key   = tableId + '_' + colIndex;
  const asc   = !_sortState[key];
  _sortState[key] = asc;

  rows.sort((a, b) => {
    const aText = a.cells[colIndex]?.textContent.trim() ?? '';
    const bText = b.cells[colIndex]?.textContent.trim() ?? '';
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

// ── Verdict filter ────────────────────────────────────────────────────────────
function filterTable(tableId, verdict, btn) {
  const table = document.getElementById(tableId);
  if (!table) return;

  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');

  table.querySelectorAll('tbody tr').forEach(row => {
    const rowVerdict = row.dataset.verdict || '';
    if (verdict === 'ALL' || rowVerdict === verdict) {
      row.classList.remove('hidden-row');
      row.style.display = '';
    } else {
      row.classList.add('hidden-row');
      row.style.display = 'none';
    }
  });
}

// ── Player name search ────────────────────────────────────────────────────────
function searchTable(tableId, query) {
  const table = document.getElementById(tableId);
  if (!table) return;
  const lq = query.toLowerCase();
  table.querySelectorAll('tbody tr').forEach(row => {
    const name = row.cells[0]?.textContent.toLowerCase() ?? '';
    if (name.includes(lq)) {
      row.style.display = '';
    } else {
      row.style.display = 'none';
    }
  });
}

// ── Date navigation ───────────────────────────────────────────────────────────
function navigateToDate(dateStr) {
  if (dateStr) window.location.href = '/date/' + dateStr;
}
