/* Baby Monitor Dashboard — Frontend Logic */

const ET_OFFSET = -4; // EDT offset hours

function toET(isoStr) {
  const d = new Date(isoStr);
  return new Date(d.getTime() + ET_OFFSET * 3600000);
}

function formatTimeET(isoStr) {
  const d = new Date(isoStr);
  return d.toLocaleTimeString('en-US', {
    timeZone: 'America/New_York',
    hour: 'numeric',
    minute: '2-digit',
    hour12: true
  });
}

function formatDateTimeET(isoStr) {
  const d = new Date(isoStr);
  return d.toLocaleString('en-US', {
    timeZone: 'America/New_York',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    hour12: true
  });
}

function frameUrl(framePath) {
  if (!framePath) return null;
  return '/api/frame?path=' + encodeURIComponent(framePath);
}

// ---------------------------------------------------------------------------
// Status bar
// ---------------------------------------------------------------------------
async function loadStatus() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();

    const iconEl = document.getElementById('status-icon');
    const iconMap = { asleep: '\u{1F7E2}', awake: '\u26A1', absent: '\u{1F6AB}', unknown: '\u2753' };
    iconEl.textContent = iconMap[data.icon] || '?';
    iconEl.className = 'status-icon ' + data.icon;

    document.getElementById('status-display').textContent = data.display;
    document.getElementById('status-duration').textContent =
      data.display + ' for ' + data.duration;

    // Alerts
    const alertsEl = document.getElementById('status-alerts');
    alertsEl.innerHTML = '';
    (data.alerts || []).forEach(a => {
      const badge = document.createElement('span');
      badge.className = 'alert-badge';
      badge.textContent = a;
      alertsEl.appendChild(badge);
    });

    // Frame thumbnail
    if (data.frame) {
      const thumb = document.getElementById('frame-thumb');
      thumb.src = frameUrl(data.frame);
      thumb.style.display = 'block';
      thumb.onclick = () => showFrameModal(data.frame);
    }

    // Meta
    if (data.timestamp) {
      document.getElementById('status-time').textContent =
        'Last capture: ' + formatDateTimeET(data.timestamp);
    }
    if (data.secondsSinceCapture != null) {
      const mins = Math.floor(data.secondsSinceCapture / 60);
      const health = mins < 10 ? '\u2705 System OK' : '\u26A0\uFE0F ' + mins + 'm since last capture';
      document.getElementById('status-health').textContent = health;
    }
  } catch (e) {
    document.getElementById('status-display').textContent = 'Error loading status';
    console.error(e);
  }
}

function showFrameModal(framePath) {
  const modal = document.getElementById('frame-modal');
  document.getElementById('frame-full').src = frameUrl(framePath);
  modal.style.display = 'flex';
  modal.onclick = () => { modal.style.display = 'none'; };
}

// ---------------------------------------------------------------------------
// Timeline
// ---------------------------------------------------------------------------
let timelineDate = null; // null = today (live), 'YYYY-MM-DD' = specific date

function initTimelineNav() {
  const picker = document.getElementById('tl-date');
  // Default to today
  const today = new Date().toLocaleDateString('en-CA', { timeZone: 'America/New_York' });
  picker.value = today;
  picker.max = today;

  picker.addEventListener('change', () => {
    timelineDate = picker.value;
    loadTimeline();
  });
  document.getElementById('tl-prev').addEventListener('click', () => {
    const d = new Date(picker.value + 'T12:00:00');
    d.setDate(d.getDate() - 1);
    picker.value = d.toISOString().slice(0, 10);
    timelineDate = picker.value;
    loadTimeline();
  });
  document.getElementById('tl-next').addEventListener('click', () => {
    const d = new Date(picker.value + 'T12:00:00');
    d.setDate(d.getDate() + 1);
    const todayStr = new Date().toLocaleDateString('en-CA', { timeZone: 'America/New_York' });
    if (d.toISOString().slice(0, 10) > todayStr) return;
    picker.value = d.toISOString().slice(0, 10);
    timelineDate = picker.value;
    loadTimeline();
  });
  document.getElementById('tl-today').addEventListener('click', () => {
    timelineDate = null;
    picker.value = new Date().toLocaleDateString('en-CA', { timeZone: 'America/New_York' });
    loadTimeline();
  });
}

async function loadTimeline() {
  try {
    const url = timelineDate
      ? '/api/timeline?date=' + timelineDate
      : '/api/timeline?hours=24';
    const res = await fetch(url);
    const data = await res.json();
    const entries = data.entries || [];

    if (entries.length === 0) {
      document.getElementById('timeline-bar').innerHTML =
        '<div style="padding:8px;color:var(--text-dim)">No data for this date</div>';
      document.getElementById('timeline-labels').innerHTML = '';
      return;
    }

    // For a specific date: midnight to midnight ET
    // For today/live: last 24h
    let start, end;
    if (timelineDate) {
      // Parse as ET midnight
      start = new Date(timelineDate + 'T00:00:00');
      // Adjust for ET offset (approximate — good enough for display)
      const etNow = new Date().toLocaleString('en-US', { timeZone: 'America/New_York' });
      const etOffset = new Date(etNow).getTimezoneOffset();
      start = new Date(timelineDate + 'T00:00:00-04:00'); // EDT
      end = new Date(start.getTime() + 24 * 3600000);
    } else {
      end = new Date();
      start = new Date(end.getTime() - 24 * 3600000);
    }
    const totalMs = 24 * 3600000;

    // Build labels (every 3h)
    const labelsEl = document.getElementById('timeline-labels');
    labelsEl.innerHTML = '';
    for (let h = 0; h <= 24; h += 3) {
      const t = new Date(start.getTime() + h * 3600000);
      const label = document.createElement('span');
      label.textContent = t.toLocaleTimeString('en-US', {
        timeZone: 'America/New_York', hour: 'numeric', hour12: true
      });
      labelsEl.appendChild(label);
    }

    // Build blocks
    const barEl = document.getElementById('timeline-bar');
    barEl.innerHTML = '';

    // Merge consecutive entries with same state category into blocks
    // Treat Unknown as Asleep for timeline display (vision model noise)
    function stateCategory(e) {
      if (!e.babyPresent) return 'absent';
      if (e.state === 'Awake') return 'awake';
      return 'asleep'; // Asleep + Unknown both show as sleep
    }

    const merged = [];
    const timelineEnd = timelineDate ? end : new Date();
    for (let i = 0; i < entries.length; i++) {
      const e = entries[i];
      const cat = stateCategory(e);
      const eTime = new Date(e.timestamp);
      const nextTime = i + 1 < entries.length ? new Date(entries[i + 1].timestamp) : timelineEnd;

      if (merged.length > 0 && merged[merged.length - 1].cat === cat) {
        merged[merged.length - 1].end = nextTime;
        merged[merged.length - 1].entries.push(e);
      } else {
        merged.push({ cat, start: eTime, end: nextTime,
          label: !e.babyPresent ? 'Out of bassinet' : (e.state || 'In bassinet'),
          entries: [e] });
      }
    }

    for (const seg of merged) {
      const blockStart = Math.max(seg.start.getTime(), start.getTime());
      const blockEnd = Math.min(seg.end.getTime(), timelineEnd.getTime());
      if (blockEnd <= blockStart) continue;

      const widthPct = ((blockEnd - blockStart) / totalMs) * 100;
      const durMin = Math.round((blockEnd - blockStart) / 60000);
      const durStr = durMin >= 60 ? Math.floor(durMin / 60) + 'h ' + (durMin % 60) + 'm' : durMin + 'm';

      const block = document.createElement('div');
      block.className = 'tl-block ' + seg.cat;
      block.style.width = widthPct + '%';

      block.title = seg.label + '\n' +
        formatTimeET(seg.start.toISOString()) + ' → ' +
        formatTimeET(seg.end.toISOString()) + '\n' +
        'Duration: ' + durStr + '\n(click for details)';
      block.style.cursor = 'pointer';
      block.addEventListener('click', () => showBlockDetail(seg, durStr));
      barEl.appendChild(block);
    }

    // Feed markers removed
  } catch (e) {
    console.error('Timeline error:', e);
  }
}

// ---------------------------------------------------------------------------
// Block detail panel — frame-by-frame viewer with prev/next
// ---------------------------------------------------------------------------
let viewerEntries = [];
let viewerIndex = 0;
let lastTrainedAt = null; // fetched once per block open

async function showBlockDetail(seg, durStr) {
  const panel = document.getElementById('block-detail');
  const summary = document.getElementById('block-detail-summary');

  summary.innerHTML =
    '<strong>' + seg.label + '</strong> &mdash; ' +
    formatTimeET(seg.start.toISOString()) + ' → ' +
    formatTimeET(seg.end.toISOString()) +
    ' (' + durStr + ', ' + seg.entries.length + ' frames)';

  // Fetch latest training timestamp
  try {
    const res = await fetch('/api/training-status');
    const data = await res.json();
    lastTrainedAt = data.lastTrained ? new Date(data.lastTrained) : null;
  } catch (e) { lastTrainedAt = null; }

  viewerEntries = seg.entries;
  viewerIndex = 0;
  renderViewer();

  panel.style.display = 'block';
  panel.scrollIntoView({ behavior: 'smooth' });
}

function renderViewer() {
  if (viewerEntries.length === 0) return;
  const e = viewerEntries[viewerIndex];

  // Image
  const img = document.getElementById('viewer-img');
  if (e.frame) {
    img.src = frameUrl(e.frame);
    img.style.display = 'block';
    img.onclick = () => showFrameModal(e.frame);
  } else {
    img.style.display = 'none';
  }

  // Counter
  document.getElementById('viewer-counter').textContent =
    (viewerIndex + 1) + ' / ' + viewerEntries.length;

  // Time
  document.getElementById('viewer-time').textContent = formatTimeET(e.timestamp);

  // Model prediction label
  const eyeState = e.eyeState || (e.state === 'Awake' ? 'eyes_open' : e.state === 'Asleep' ? 'eyes_closed' : 'face_not_visible');
  const labelMap = { eyes_open: 'Eyes Open', eyes_closed: 'Eyes Closed', face_not_visible: 'Face Not Visible' };
  const modelLabel = document.getElementById('viewer-model-label');
  modelLabel.textContent = 'Model: ' + (labelMap[eyeState] || eyeState);

  // Eye state dropdown
  const stateSelect = document.getElementById('viewer-state');
  stateSelect.value = eyeState;

  // Retrain status indicator
  const retrainEl = document.getElementById('viewer-retrain-status');
  if (e.eyeStateEdited || e._correctedAt) {
    const correctedAt = e._correctedAt ? new Date(e._correctedAt) : null;
    if (lastTrainedAt && correctedAt && correctedAt < lastTrainedAt) {
      retrainEl.textContent = 'retrained';
      retrainEl.className = 'viewer-retrain-status retrained';
    } else {
      retrainEl.textContent = 'pending retrain';
      retrainEl.className = 'viewer-retrain-status pending';
    }
  } else {
    retrainEl.textContent = '';
    retrainEl.className = 'viewer-retrain-status';
  }

  // Clear saved indicator
  document.getElementById('viewer-saved').textContent = '';

  // Button states
  document.getElementById('viewer-prev').disabled = viewerIndex === 0;
  document.getElementById('viewer-next').disabled = viewerIndex === viewerEntries.length - 1;
}

document.getElementById('viewer-prev').addEventListener('click', () => {
  if (viewerIndex > 0) { viewerIndex--; renderViewer(); }
});

document.getElementById('viewer-next').addEventListener('click', () => {
  if (viewerIndex < viewerEntries.length - 1) { viewerIndex++; renderViewer(); }
});

// Keyboard navigation: arrow keys
document.addEventListener('keydown', (ev) => {
  const panel = document.getElementById('block-detail');
  if (panel.style.display === 'none') return;
  if (ev.target.tagName === 'SELECT' || ev.target.tagName === 'INPUT') return;

  if (ev.key === 'ArrowLeft' && viewerIndex > 0) {
    viewerIndex--; renderViewer(); ev.preventDefault();
  } else if (ev.key === 'ArrowRight' && viewerIndex < viewerEntries.length - 1) {
    viewerIndex++; renderViewer(); ev.preventDefault();
  }
});

// Eye state change from viewer
document.getElementById('viewer-state').addEventListener('change', async (ev) => {
  const e = viewerEntries[viewerIndex];
  if (!e) return;
  const newEyeState = ev.target.value;
  const saved = document.getElementById('viewer-saved');
  saved.textContent = 'saving...';

  try {
    const res = await fetch('/api/update-entry', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ timestamp: e.timestamp, eyeState: newEyeState }),
    });
    const data = await res.json();
    if (data.ok) {
      e.eyeState = newEyeState;
      e.eyeStateEdited = true;
      e._correctedAt = new Date().toISOString();
      saved.textContent = 'saved';
      saved.style.color = '#4a9eff';
      setTimeout(() => { saved.textContent = ''; renderViewer(); }, 800);
    } else {
      saved.textContent = 'error';
      saved.style.color = '#ff5252';
    }
  } catch (err) {
    saved.textContent = 'error';
    saved.style.color = '#ff5252';
    console.error('Update error:', err);
  }
});

document.getElementById('block-detail-close').addEventListener('click', () => {
  document.getElementById('block-detail').style.display = 'none';
});

// ---------------------------------------------------------------------------
// Stats cards
// ---------------------------------------------------------------------------
async function loadStats() {
  try {
    // Sleep stats for today
    const sleepRes = await fetch('/api/sleep-stats?days=1');
    const sleepData = await sleepRes.json();
    const today = sleepData.days && sleepData.days.length > 0 ? sleepData.days[sleepData.days.length - 1] : null;

    if (today) {
      const totalH = today.totalHours;
      const hrs = Math.floor(totalH);
      const mins = Math.round((totalH - hrs) * 60);
      document.getElementById('stat-sleep-total').textContent = hrs + 'h ' + mins + 'm';

      const longestH = today.longestSleepHours || today.longestStretchHours || 0;
      const lHrs = Math.floor(longestH);
      const lMins = Math.round((longestH - lHrs) * 60);
      document.getElementById('stat-longest').textContent = lHrs + 'h ' + lMins + 'm';
    } else {
      document.getElementById('stat-sleep-total').textContent = '0h';
      document.getElementById('stat-longest').textContent = '0h';
    }

    // Feeds and diapers removed
  } catch (e) {
    console.error('Stats error:', e);
  }
}

// ---------------------------------------------------------------------------
// Sleep trends chart
// ---------------------------------------------------------------------------
let sleepChart = null;

async function loadSleepTrends() {
  try {
    const res = await fetch('/api/sleep-stats?days=14');
    const data = await res.json();
    const days = data.days || [];

    const labels = days.map(d => {
      const dt = new Date(d.date + 'T12:00:00');
      return dt.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });
    });
    const totalHours = days.map(d => d.totalHours);
    const longestSleep = days.map(d => d.longestSleepHours || d.longestStretchHours || 0);
    const longestBassinet = days.map(d => d.longestBassinetHours || 0);

    const ctx = document.getElementById('sleep-chart').getContext('2d');

    if (sleepChart) sleepChart.destroy();

    sleepChart = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: labels,
        datasets: [
          {
            label: 'Total Sleep (hours)',
            data: totalHours,
            backgroundColor: 'rgba(74, 158, 255, 0.6)',
            borderColor: 'rgba(74, 158, 255, 1)',
            borderWidth: 1,
            borderRadius: 4,
            order: 3,
          },
          {
            label: 'Longest Sleep Stretch',
            data: longestSleep,
            type: 'line',
            borderColor: 'rgba(255, 152, 0, 1)',
            backgroundColor: 'rgba(255, 152, 0, 0.1)',
            pointBackgroundColor: 'rgba(255, 152, 0, 1)',
            pointRadius: 4,
            tension: 0.3,
            fill: false,
            order: 1,
          },
          {
            label: 'Longest In Bassinet',
            data: longestBassinet,
            type: 'line',
            borderColor: 'rgba(76, 175, 80, 1)',
            backgroundColor: 'rgba(76, 175, 80, 0.1)',
            pointBackgroundColor: 'rgba(76, 175, 80, 1)',
            pointRadius: 4,
            tension: 0.3,
            fill: false,
            borderDash: [5, 3],
            order: 2,
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: {
            labels: { color: '#e0e0e0', font: { size: 12 } }
          }
        },
        scales: {
          x: {
            ticks: { color: '#8892a4' },
            grid: { color: 'rgba(42, 58, 92, 0.5)' }
          },
          y: {
            beginAtZero: true,
            max: 20,
            ticks: {
              color: '#8892a4',
              callback: v => v + 'h'
            },
            grid: { color: 'rgba(42, 58, 92, 0.5)' }
          }
        }
      }
    });
  } catch (e) {
    console.error('Chart error:', e);
  }
}

// ---------------------------------------------------------------------------
// Recent events table
// ---------------------------------------------------------------------------
async function loadEvents() {
  try {
    const res = await fetch('/api/events');
    const data = await res.json();
    const events = data.events || [];

    const tbody = document.getElementById('events-body');
    tbody.innerHTML = '';

    events.forEach(ev => {
      const tr = document.createElement('tr');

      const tdTime = document.createElement('td');
      tdTime.textContent = formatDateTimeET(ev.timestamp);
      tr.appendChild(tdTime);

      const tdType = document.createElement('td');
      const badge = document.createElement('span');
      badge.className = 'event-badge';
      if (ev.type.includes('Placed')) badge.classList.add('placed');
      else if (ev.type.includes('Removed')) badge.classList.add('removed');
      else if (ev.type.includes('asleep') || ev.type.includes('Fell')) badge.classList.add('asleep');
      else if (ev.type.includes('Woke')) badge.classList.add('woke');
      else badge.classList.add('other');
      badge.textContent = ev.type;
      tdType.appendChild(badge);
      tr.appendChild(tdType);

      const tdDur = document.createElement('td');
      tdDur.textContent = ev.duration || '—';
      tr.appendChild(tdDur);

      tbody.appendChild(tr);
    });
  } catch (e) {
    console.error('Events error:', e);
  }
}

// ---------------------------------------------------------------------------
// Training status & retrain button
// ---------------------------------------------------------------------------
async function loadTrainingStatus() {
  try {
    const res = await fetch('/api/training-status');
    const data = await res.json();
    const el = document.getElementById('footer-model');
    if (data.lastTrained) {
      const dt = new Date(data.lastTrained);
      const timeStr = dt.toLocaleString('en-US', {
        timeZone: 'America/New_York',
        month: 'short', day: 'numeric',
        hour: 'numeric', minute: '2-digit', hour12: true,
      });
      el.textContent = 'Model ' + (data.version || '') + ' — trained ' + timeStr;
    } else {
      el.textContent = 'No model trained yet';
    }
  } catch (e) {
    console.error('Training status error:', e);
  }
}

document.getElementById('footer-retrain').addEventListener('click', async () => {
  const btn = document.getElementById('footer-retrain');
  btn.disabled = true;
  btn.textContent = 'Retraining...';

  try {
    const res = await fetch('/api/retrain', { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      btn.textContent = 'Retrain started';
      // Poll for completion (training-status will update when done)
      const poll = setInterval(async () => {
        await loadTrainingStatus();
        const el = document.getElementById('footer-model');
        // Check if version changed
        if (el.textContent.includes('trained')) {
          clearInterval(poll);
          btn.textContent = 'Retrain Model';
          btn.disabled = false;
        }
      }, 10000); // check every 10s
      // Safety timeout: re-enable after 15 min regardless
      setTimeout(() => { clearInterval(poll); btn.textContent = 'Retrain Model'; btn.disabled = false; }, 900000);
    }
  } catch (e) {
    btn.textContent = 'Retrain failed';
    btn.disabled = false;
    console.error('Retrain error:', e);
  }
});

// ---------------------------------------------------------------------------
// Init & auto-refresh
// ---------------------------------------------------------------------------
async function loadAll() {
  await Promise.all([
    loadStatus(),
    loadTimeline(),
    loadStats(),
    loadSleepTrends(),
    loadEvents(),
    loadTrainingStatus(),
  ]);
  document.getElementById('footer-refresh').textContent =
    'Last refreshed: ' + new Date().toLocaleTimeString('en-US', { timeZone: 'America/New_York' });
}

initTimelineNav();
loadAll();
setInterval(loadAll, 60000);
