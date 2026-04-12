// live.js — Polls /api/live every 30 seconds and updates the Live Scores tab.

(function () {
  const POLL_INTERVAL_MS = 30000;
  let expandedGame = null;

  function fetchLive() {
    fetch('/api/live')
      .then(r => r.json())
      .then(data => renderScores(data))
      .catch(() => {});
  }

  function renderScores(data) {
    const container = document.getElementById('live-scores-container');
    if (!container) return;

    const events = data.events || [];
    if (!events.length) {
      container.innerHTML = '<div class="empty-state">No live games right now.</div>';
      return;
    }

    container.innerHTML = '';
    events.forEach(evt => {
      const comp   = evt.competitions?.[0] || {};
      const comps  = comp.competitors || [];
      const home   = comps.find(c => c.homeAway === 'home') || comps[1] || {};
      const away   = comps.find(c => c.homeAway === 'away') || comps[0] || {};
      const status = evt.status?.type?.description || 'Scheduled';
      const detail = evt.status?.type?.shortDetail || '';
      const gamePk = evt.id;

      const card = document.createElement('div');
      card.className = 'live-game-card';
      card.dataset.gamePk = gamePk;

      card.innerHTML = `
        <div class="live-game-score">
          <span>${away.team?.abbreviation || '?'} ${away.score ?? ''}</span>
          <span class="muted">—</span>
          <span>${home.team?.abbreviation || '?'} ${home.score ?? ''}</span>
          <span class="game-status status-${status.toLowerCase().includes('final') ? 'final' : status.toLowerCase().includes('progress') ? 'live' : 'scheduled'}">
            ${status.toLowerCase().includes('final') ? 'FINAL' : status.toUpperCase()}
          </span>
        </div>
        <div class="live-game-meta">${detail}</div>
        <div class="live-expanded" id="live-expanded-${gamePk}"></div>
      `;

      card.addEventListener('click', () => toggleExpanded(gamePk, card));
      container.appendChild(card);
    });

    // Re-expand if previously expanded
    if (expandedGame) {
      const card = document.querySelector(`[data-game-pk="${expandedGame}"]`);
      if (card) fetchGameDetail(expandedGame, card);
    }
  }

  function toggleExpanded(gamePk, card) {
    const expanded = document.getElementById('live-expanded-' + gamePk);
    if (!expanded) return;
    const isOpen = expanded.classList.contains('open');
    // Close all
    document.querySelectorAll('.live-expanded.open').forEach(el => el.classList.remove('open'));
    if (isOpen) {
      expandedGame = null;
    } else {
      expanded.classList.add('open');
      expandedGame = gamePk;
      fetchGameDetail(gamePk, card);
    }
  }

  function fetchGameDetail(gamePk, card) {
    fetch('/api/live/game/' + gamePk)
      .then(r => r.json())
      .then(data => renderPitchLog(gamePk, data.pitches || []))
      .catch(() => {});
  }

  function renderPitchLog(gamePk, pitches) {
    const el = document.getElementById('live-expanded-' + gamePk);
    if (!el) return;
    if (!pitches.length) {
      el.innerHTML = '<div class="muted" style="font-size:0.78rem;padding:0.5rem">No pitch data yet.</div>';
      return;
    }
    let html = `
      <table class="data-table" style="margin-top:0.5rem">
        <thead><tr>
          <th>#</th><th>Type</th><th>Speed</th><th>Zone</th><th>Result</th><th>Count</th>
        </tr></thead>
        <tbody>
    `;
    pitches.slice(-20).reverse().forEach(p => {
      html += `<tr>
        <td class="mono">${p.pitch_number}</td>
        <td>${p.pitch_type || '—'}</td>
        <td class="mono">${p.speed ? p.speed.toFixed(1) : '—'}</td>
        <td class="mono">${p.zone || '—'}</td>
        <td>${p.description || '—'}</td>
        <td class="mono">${p.balls}-${p.strikes}</td>
      </tr>`;
    });
    html += '</tbody></table>';
    el.innerHTML = html;
  }

  // Initial fetch + polling
  fetchLive();
  setInterval(fetchLive, POLL_INTERVAL_MS);
})();
