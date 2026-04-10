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

function formatSeconds(sec) {
  if (sec == null || isNaN(sec)) return '—';
  if (sec < 60) return sec.toFixed(1) + 's';
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  if (m < 60) return m + 'm ' + s + 's';
  const h = Math.floor(m / 60);
  return h + 'h ' + (m % 60) + 'm';
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

    // Live frame (hero image)
    const thumb = document.getElementById('frame-thumb');
    const placeholder = document.getElementById('live-frame-placeholder');
    if (data.frame) {
      thumb.src = frameUrl(data.frame);
      thumb.style.display = 'block';
      thumb.onclick = () => showFrameModal(data.frame);
      if (placeholder) placeholder.style.display = 'none';
    }

    // Store capture time for countdown timer
    if (data.secondsSinceCapture != null) {
      lastCaptureAgoSec = data.secondsSinceCapture;
      lastCaptureCheckedAt = Date.now();
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
      updateTimelineStats([]);
      return;
    }

    updateTimelineStats(entries);

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

    // Store blocks with their durStr for block-level navigation
    allBlocks = [];
    for (const seg of merged) {
      const blockStart = Math.max(seg.start.getTime(), start.getTime());
      const blockEnd = Math.min(seg.end.getTime(), timelineEnd.getTime());
      if (blockEnd <= blockStart) continue;

      const widthPct = ((blockEnd - blockStart) / totalMs) * 100;
      const durMin = Math.round((blockEnd - blockStart) / 60000);
      const durStr = durMin >= 60 ? Math.floor(durMin / 60) + 'h ' + (durMin % 60) + 'm' : durMin + 'm';

      seg._durStr = durStr;
      allBlocks.push(seg);
      const blockIdx = allBlocks.length - 1;

      const block = document.createElement('div');
      block.className = 'tl-block ' + seg.cat;
      block.style.width = widthPct + '%';

      block.title = seg.label + '\n' +
        formatTimeET(seg.start.toISOString()) + ' → ' +
        formatTimeET(seg.end.toISOString()) + '\n' +
        'Duration: ' + durStr + '\n(click for details)';
      block.style.cursor = 'pointer';
      block.addEventListener('click', () => openBlock(blockIdx));
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
let trainingData = null; // shared training state from API
let allBlocks = [];      // all merged timeline blocks (for block prev/next)
let currentBlockIndex = -1;

function openBlock(idx) {
  currentBlockIndex = idx;
  const seg = allBlocks[idx];
  showBlockDetail(seg, seg._durStr);
}

function showBlockDetail(seg, durStr) {
  const panel = document.getElementById('block-detail');
  const summary = document.getElementById('block-detail-summary');

  // Count pending retrains in this block
  const lastTrained = trainingData && trainingData.lastTrained ? new Date(trainingData.lastTrained) : null;
  const pendingInBlock = seg.entries.filter(e => {
    const cat = e.eyeStateCorrectedAt || e._correctedAt;
    if (!cat) return false;
    return !lastTrained || new Date(cat) > lastTrained;
  }).length;
  const pendingBadge = pendingInBlock > 0
    ? ' <span class="block-pending-badge">' + pendingInBlock + ' pending retrain</span>'
    : '';

  summary.innerHTML =
    '<strong>' + seg.label + '</strong> &mdash; ' +
    formatTimeET(seg.start.toISOString()) + ' → ' +
    formatTimeET(seg.end.toISOString()) +
    ' (' + durStr + ', ' + seg.entries.length + ' frames)' + pendingBadge;

  // Block nav buttons
  document.getElementById('block-prev').disabled = currentBlockIndex <= 0;
  document.getElementById('block-next').disabled = currentBlockIndex >= allBlocks.length - 1;
  document.getElementById('block-counter').textContent =
    'Block ' + (currentBlockIndex + 1) + ' / ' + allBlocks.length;

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

  // Time + detection method + model version
  const methodMap = { birdeye: 'birdeye', 'vision-api': 'cloud', 'openai-vision': 'cloud', 'pixel-diff': 'pixel-diff' };
  const method = methodMap[e.detectionMethod] || e.detectionMethod || '?';
  let timeText = formatTimeET(e.timestamp) + '  ·  ' + method;
  if (e.shadowModelVersion) {
    timeText += '  ·  ' + e.shadowModelVersion;
  }
  document.getElementById('viewer-time').textContent = timeText;

  // Model prediction label
  const eyeState = e.eyeState || (!e.babyPresent ? 'not_in_bassinet' : e.state === 'Awake' ? 'eyes_open' : e.state === 'Asleep' ? 'eyes_closed' : 'face_not_visible');
  const labelMap = { eyes_open: 'Eyes Open', eyes_closed: 'Eyes Closed', face_not_visible: 'Face Not Visible', not_in_bassinet: 'Not In Bassinet' };
  const modelLabel = document.getElementById('viewer-model-label');
  modelLabel.textContent = 'Cloud: ' + (labelMap[eyeState] || eyeState);

  // BIRDEYE shadow classifier labels (presence + eye state)
  const presenceEl = document.getElementById('viewer-birdeye-presence');
  const eyeEl = document.getElementById('viewer-birdeye-eye');
  const fmtConf = (c) => (c == null ? '' : ' (' + Math.round(c * 100) + '%)');
  if (e.shadowBirdeyeState != null) {
    const presence = e.shadowBirdeyeState === 'not_present' ? 'not_present' : 'present';
    presenceEl.textContent = 'BIRDEYE presence: ' + presence + fmtConf(e.shadowPresenceConfidence);
    presenceEl.style.display = '';
    const agreedPresence = (presence === 'present') === !!e.babyPresent;
    presenceEl.classList.toggle('disagree', !agreedPresence);
  } else {
    presenceEl.style.display = 'none';
    presenceEl.classList.remove('disagree');
  }
  if (e.shadowEyeState != null) {
    eyeEl.textContent = 'BIRDEYE eyes: ' + (labelMap[e.shadowEyeState] || e.shadowEyeState) + fmtConf(e.shadowEyeConfidence);
    eyeEl.style.display = '';
    const agreedEye = e.shadowEyeState === eyeState;
    eyeEl.classList.toggle('disagree', !agreedEye);
  } else if (e.shadowBirdeyeState === 'not_present') {
    // Eye classifier skipped (no baby present)
    eyeEl.textContent = 'BIRDEYE eyes: — (skipped)';
    eyeEl.style.display = '';
    eyeEl.classList.remove('disagree');
  } else {
    eyeEl.style.display = 'none';
    eyeEl.classList.remove('disagree');
  }

  // Eye state dropdown
  const stateSelect = document.getElementById('viewer-state');
  stateSelect.value = eyeState;

  // Retrain status indicator (derived from training API data)
  const retrainEl = document.getElementById('viewer-retrain-status');
  const correctedAtStr = e.eyeStateCorrectedAt || e._correctedAt;
  const lastTrainedStr = trainingData && trainingData.lastTrained;
  if (e.eyeStateEdited || correctedAtStr) {
    const correctedAt = correctedAtStr ? new Date(correctedAtStr) : null;
    const lastTrained = lastTrainedStr ? new Date(lastTrainedStr) : null;
    if (lastTrained && correctedAt && correctedAt < lastTrained) {
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

// Block-level label override
document.getElementById('block-label-apply').addEventListener('click', async () => {
  const select = document.getElementById('block-label-select');
  const status = document.getElementById('block-label-status');
  const newEyeState = select.value;

  if (!newEyeState || viewerEntries.length === 0) return;

  const btn = document.getElementById('block-label-apply');
  btn.disabled = true;
  status.textContent = '0/' + viewerEntries.length;
  status.style.color = 'var(--text-dim)';

  let success = 0;
  let failed = 0;

  for (let i = 0; i < viewerEntries.length; i++) {
    const e = viewerEntries[i];
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
        success++;
      } else {
        failed++;
      }
    } catch (err) {
      failed++;
    }
    status.textContent = (i + 1) + '/' + viewerEntries.length;
  }

  if (failed === 0) {
    status.textContent = 'All ' + success + ' frames updated';
    status.style.color = 'var(--accent-blue)';
  } else {
    status.textContent = success + ' updated, ' + failed + ' failed';
    status.style.color = 'var(--accent-orange)';
  }

  btn.disabled = false;
  select.value = '';
  renderViewer(); // refresh current frame display
  setTimeout(() => { status.textContent = ''; }, 3000);
});
document.getElementById('block-prev').addEventListener('click', () => {
  if (currentBlockIndex > 0) openBlock(currentBlockIndex - 1);
});
document.getElementById('block-next').addEventListener('click', () => {
  if (currentBlockIndex < allBlocks.length - 1) openBlock(currentBlockIndex + 1);
});

// ---------------------------------------------------------------------------
// Timeline stats (in-bassinet vs out, computed from timeline entries)
// ---------------------------------------------------------------------------
function updateTimelineStats(entries) {
  if (!entries || entries.length === 0) {
    document.getElementById('stat-in-bassinet').textContent = '--';
    document.getElementById('stat-out-bassinet').textContent = '--';
    return;
  }

  let inMs = 0;
  let outMs = 0;

  for (let i = 0; i < entries.length; i++) {
    const e = entries[i];
    const eTime = new Date(e.timestamp).getTime();
    const nextTime = i + 1 < entries.length
      ? new Date(entries[i + 1].timestamp).getTime()
      : eTime; // last entry: no duration to add

    if (i + 1 >= entries.length) continue;
    const dur = nextTime - eTime;

    if (e.babyPresent) {
      inMs += dur;
    } else {
      outMs += dur;
    }
  }

  const totalMs = inMs + outMs;
  const fmtDur = (ms) => {
    const totalMin = Math.round(ms / 60000);
    const h = Math.floor(totalMin / 60);
    const m = totalMin % 60;
    return h > 0 ? h + 'h ' + m + 'm' : m + 'm';
  };
  const pct = (ms) => totalMs > 0 ? Math.round(ms / totalMs * 100) + '%' : '0%';

  document.getElementById('stat-in-bassinet').textContent = fmtDur(inMs) + ' (' + pct(inMs) + ')';
  document.getElementById('stat-out-bassinet').textContent = fmtDur(outMs) + ' (' + pct(outMs) + ')';
}


// ---------------------------------------------------------------------------
// Training stats (populated from trainingData after loadTrainingStatus)
// ---------------------------------------------------------------------------
function renderTrainingStats() {
  const section = document.getElementById('training-stats');
  if (!trainingData || !trainingData.lastMetrics) {
    section.style.display = 'none';
    return;
  }
  section.style.display = '';

  const m = trainingData.lastMetrics;
  const sources = trainingData.lastLabelSources || {};
  const total = trainingData.lastEntriesTotal || 0;

  // Version + trained time + run status
  const trainedAt = trainingData.lastTrained
    ? new Date(trainingData.lastTrained).toLocaleString('en-US', {
        timeZone: 'America/New_York', month: 'short', day: 'numeric',
        hour: 'numeric', minute: '2-digit', hour12: true })
    : '?';

  let statusBadge = '';
  if (trainingData.running) {
    statusBadge = ' <span style="color:var(--accent-blue);font-size:0.8rem">⟳ training now</span>';
  } else if (trainingData.runStatus === 'aborted') {
    statusBadge = ' <span style="color:var(--accent-red);font-size:0.8rem">✕ aborted</span>';
  } else if (trainingData.runStatus === 'failed') {
    statusBadge = ' <span style="color:var(--accent-red);font-size:0.8rem">✕ failed</span>';
  } else if (trainingData.runStatus === 'completed' && trainingData.finishedAt) {
    const finAt = new Date(trainingData.finishedAt).toLocaleString('en-US', {
      timeZone: 'America/New_York', month: 'short', day: 'numeric',
      hour: 'numeric', minute: '2-digit', hour12: true });
    let durStr = '';
    if (trainingData.startedAt && trainingData.finishedAt) {
      const durSec = Math.round((new Date(trainingData.finishedAt) - new Date(trainingData.startedAt)) / 1000);
      durStr = durSec >= 60 ? ' in ' + Math.floor(durSec / 60) + 'm ' + (durSec % 60) + 's' : ' in ' + durSec + 's';
    }
    statusBadge = ' <span style="color:var(--accent-green);font-size:0.8rem">✓ last run ' + finAt + durStr + '</span>';
  }

  document.getElementById('train-version').innerHTML =
    (trainingData.version || '?') + ' — trained ' + trainedAt + statusBadge;

  // Data column
  const dataEl = document.getElementById('train-data');
  const srcRows = Object.entries(sources).map(([k,v]) =>
    '<div class="train-row"><span>' + k + '</span><span class="train-val">' + v + '</span></div>'
  ).join('');

  // Live alignment from the perf-agreement element
  const liveAlignment = document.getElementById('perf-agreement').textContent;
  const alignColor = document.getElementById('perf-agreement').style.color || 'var(--text)';

  let prevInfo = '';
  if (trainingData.prevVersion) {
    prevInfo = '<div class="train-row"><span title="Previous model for delta comparison">Previous model</span><span class="train-val">' + trainingData.prevVersion + '</span></div>';
  }

  // Corrections counts
  const pending = trainingData.pendingCorrections || 0;
  const totalCorr = trainingData.totalCorrections || 0;
  const trained = totalCorr - pending;
  const pendingColor = pending > 0 ? 'var(--accent-orange)' : 'var(--text-dim)';
  const pendingRow = '<div class="train-row"><span title="Corrections made since last training — will be included in the next retrain">Pending changes</span><span class="train-val" style="color:' + pendingColor + '">' + pending + '</span></div>' +
    '<div class="train-row"><span title="Corrections already used in previous training runs">Previous changes</span><span class="train-val">' + trained + '</span></div>';

  // Run timing: last run + avg/p99 across all recorded runs
  const durStats = trainingData.trainingDurationStats || {};
  const lastDurSec = trainingData.lastDurationSeconds;
  const runCount = durStats.count || 0;
  const lastRow = lastDurSec != null
    ? '<div class="train-row"><span title="Wall-clock duration of the most recent training run">Last run</span><span class="train-val">' + formatSeconds(lastDurSec) + '</span></div>'
    : '';
  const avgRow = durStats.avg_seconds != null
    ? '<div class="train-row"><span title="Average wall-clock duration across ' + runCount + ' recorded training runs">Avg training time</span><span class="train-val">' + formatSeconds(durStats.avg_seconds) + '</span></div>'
    : '';
  const p99Row = durStats.p99_seconds != null
    ? '<div class="train-row"><span title="99th-percentile training duration across ' + runCount + ' recorded training runs (linear interpolation)">p99 training time</span><span class="train-val">' + formatSeconds(durStats.p99_seconds) + '</span></div>'
    : '';
  const timingHeader = (lastRow || avgRow || p99Row)
    ? '<div style="margin:8px 0 2px;font-size:0.7rem;color:#445" title="Wall-clock duration of train_classifiers.py runs">Run timing:</div>'
    : '';

  dataEl.innerHTML =
    '<div class="train-row"><span title="How often birdeye matches ground truth on live production frames">Live alignment</span><span class="train-val" style="color:' + alignColor + '">' + liveAlignment + '</span></div>' +
    pendingRow +
    prevInfo +
    timingHeader + lastRow + avgRow + p99Row +
    '<div style="margin:8px 0 2px;font-size:0.7rem;color:#445" title="Data used in the last training run">Training data:</div>' +
    '<div class="train-row"><span title="Total labeled frames fed to the trainer">Total entries</span><span class="train-val">' + total + '</span></div>' +
    srcRows;

  // Delta helper: show change from previous training
  function delta(curr, prev, suffix, higherIsBetter) {
    if (prev == null || curr == null) return '';
    const diff = curr - prev;
    if (Math.abs(diff) < 0.001) return '';
    const sign = diff > 0 ? '+' : '';
    const good = higherIsBetter ? diff > 0 : diff < 0;
    const color = good ? 'var(--accent-green)' : 'var(--accent-red)';
    return ' <span style="font-size:0.7rem;color:' + color + '">' + sign + (diff * 100).toFixed(1) + suffix + '</span>';
  }

  const prev = trainingData.prevMetrics || {};

  // Helper to render per-class metrics with definitions and deltas
  function renderClassifier(el, metrics, prevMetrics) {
    if (!metrics) { el.innerHTML = 'No data'; return; }
    const pm = prevMetrics || {};
    let html = '';

    html += '<div class="train-row"><span title="% of validation samples correctly classified during training">Train accuracy</span><span class="train-val">'
      + (metrics.val_accuracy * 100).toFixed(1) + '%'
      + delta(metrics.val_accuracy, pm.val_accuracy, '%', true)
      + '</span></div>';

    html += '<div class="train-row"><span title="Cross-entropy loss on validation set (with class weights). Tracked but not used for best-model selection.">Val loss</span><span class="train-val">'
      + metrics.best_val_loss
      + delta(metrics.best_val_loss, pm.best_val_loss, '', false)
      + '</span></div>';

    if (metrics.best_macro_f1 != null) {
      html += '<div class="train-row"><span title="Macro-averaged F1 across all classes (equal weight per class). Best-model selection criterion — picks the epoch with the highest value.">Macro F1</span><span class="train-val">'
        + (metrics.best_macro_f1 * 100).toFixed(1) + '%'
        + delta(metrics.best_macro_f1, pm.best_macro_f1, '%', true)
        + '</span></div>';
    }

    html += '<div class="train-row"><span title="Epoch with best macro-F1 / total epochs before early stopping">Epochs</span><span class="train-val">'
      + metrics.best_epoch + ' / ' + metrics.total_epochs + '</span></div>';

    // Presence classifier stats
    if (metrics.out_labeled_as_in != null) {
      html += '<div class="train-row"><span title="Bassinet was empty but model said baby present (false positive)">Out labeled as In</span><span class="train-val">' + metrics.out_labeled_as_in + '</span></div>';
    }
    if (metrics.in_labeled_as_out != null) {
      html += '<div class="train-row"><span title="Baby was present but model said empty (false negative — misses baby)">In labeled as Out</span><span class="train-val">' + metrics.in_labeled_as_out + '</span></div>';
    }
    if (metrics.class_split) {
      const cs = metrics.class_split;
      html += '<div class="train-row"><span title="How many present vs not_present samples in the validation set">Class split</span><span class="train-val">' + cs.present + ' in / ' + cs.not_present + ' out (' + cs.pct_present + '% present)</span></div>';
    }
    if (metrics.total_val_labels != null) {
      html += '<div class="train-row"><span title="Total validation samples">Total val labels</span><span class="train-val">' + metrics.total_val_labels + '</span></div>';
    }

    if (metrics.awake_asleep_miss_rate != null) {
      const missClass = metrics.awake_asleep_miss_rate > 0.05 ? 'train-crit' : 'train-val';
      html += '<div class="train-row"><span title="CRITICAL: % of truly-awake frames the model predicted as asleep. Must be &lt;5% for safety.">Awake→Asleep misses</span><span class="' + missClass + '">'
        + metrics.awake_asleep_misses + ' (' + (metrics.awake_asleep_miss_rate * 100).toFixed(0) + '%)'
        + delta(metrics.awake_asleep_miss_rate, pm.awake_asleep_miss_rate, '%', false)
        + '</span></div>';
    }

    if (metrics.asleep_awake_false_alarm_rate != null) {
      html += '<div class="train-row"><span title="% of truly-asleep frames the model predicted as awake. Causes unnecessary alerts but not dangerous.">Asleep→Awake false alarms</span><span class="train-val">'
        + metrics.asleep_awake_false_alarms + ' (' + (metrics.asleep_awake_false_alarm_rate * 100).toFixed(0) + '%)'
        + delta(metrics.asleep_awake_false_alarm_rate, pm.asleep_awake_false_alarm_rate, '%', false)
        + '</span></div>';
    }

    if (metrics.per_class) {
      html += '<div style="margin-top:6px;font-size:0.75rem;color:var(--text-dim)" title="P=precision (of predicted X, how many were truly X), R=recall (of truly X, how many were found), F1=harmonic mean">Per-class P / R / F1:</div>';
      for (const [cls, s] of Object.entries(metrics.per_class)) {
        const ps = pm.per_class && pm.per_class[cls];
        html += '<div class="train-row"><span>' + cls + ' <span style="color:#556">(' + s.support + ')</span></span><span class="train-val">'
          + s.precision + ' / ' + s.recall + ' / ' + s.f1
          + (ps ? delta(s.f1, ps.f1, '', true) : '')
          + '</span></div>';
      }
    }
    el.innerHTML = html;
  }

  renderClassifier(document.getElementById('train-presence'), m.presence, prev.presence);
  renderClassifier(document.getElementById('train-eye'), m.eye_state, prev.eye_state);
}

// ---------------------------------------------------------------------------
// Recent events table
// ---------------------------------------------------------------------------
async function loadEvents() {
  try {
    const count = document.getElementById('events-count').value;
    const res = await fetch('/api/events?count=' + count);
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
// Training status & retrain/abort buttons
// ---------------------------------------------------------------------------
let trainPollInterval = null;

async function loadTrainingStatus() {
  try {
    const res = await fetch('/api/training-status');
    const data = await res.json();
    trainingData = data; // shared state for viewer

    // Model info line
    const el = document.getElementById('footer-model');
    if (data.lastTrained) {
      const dt = new Date(data.lastTrained);
      const timeStr = dt.toLocaleString('en-US', {
        timeZone: 'America/New_York',
        month: 'short', day: 'numeric',
        hour: 'numeric', minute: '2-digit', hour12: true,
      });
      let info = (data.version || 'model') + ' — trained ' + timeStr;
      if (data.pendingCorrections > 0) {
        info += ' — ' + data.pendingCorrections + ' pending';
      }
      el.textContent = info;
    } else {
      el.textContent = 'No model trained yet';
      if (data.pendingCorrections > 0) {
        el.textContent += ' — ' + data.pendingCorrections + ' corrections ready';
      }
    }

    // Button state
    const btn = document.getElementById('footer-retrain');
    const abortBtn = document.getElementById('footer-abort');

    if (data.running) {
      btn.style.display = 'none';
      abortBtn.style.display = '';
      const startedAt = data.startedAt ? new Date(data.startedAt) : null;
      const elapsed = startedAt ? Math.round((Date.now() - startedAt.getTime()) / 1000) : 0;
      const elapsedStr = elapsed > 60 ? Math.floor(elapsed / 60) + 'm ' + (elapsed % 60) + 's' : elapsed + 's';
      abortBtn.textContent = 'Abort (' + elapsedStr + ')';
      if (!trainPollInterval) {
        trainPollInterval = setInterval(loadTrainingStatus, 5000);
      }
    } else {
      btn.style.display = '';
      abortBtn.style.display = 'none';
      btn.disabled = false;
      btn.textContent = data.pendingCorrections > 0
        ? 'Retrain (' + data.pendingCorrections + ' new)'
        : 'Retrain Model';
      if (trainPollInterval) {
        clearInterval(trainPollInterval);
        trainPollInterval = null;
      }
    }

    renderTrainingStats();
    return data;
  } catch (e) {
    console.error('Training status error:', e);
    return null;
  }
}

document.getElementById('footer-retrain').addEventListener('click', async () => {
  const btn = document.getElementById('footer-retrain');
  btn.disabled = true;
  btn.textContent = 'Starting...';

  try {
    const res = await fetch('/api/retrain', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ trigger: 'dashboard' }),
    });
    const data = await res.json();
    if (!data.ok) {
      btn.textContent = data.error || 'Failed';
      setTimeout(() => { btn.textContent = 'Retrain Model'; btn.disabled = false; }, 3000);
      return;
    }
    // Poll will pick up the running state
    await loadTrainingStatus();
  } catch (e) {
    btn.textContent = 'Error';
    btn.disabled = false;
    console.error('Retrain error:', e);
  }
});

document.getElementById('footer-abort').addEventListener('click', async () => {
  const btn = document.getElementById('footer-abort');
  btn.disabled = true;
  btn.textContent = 'Aborting...';

  try {
    await fetch('/api/retrain/abort', { method: 'POST' });
    await loadTrainingStatus();
  } catch (e) {
    console.error('Abort error:', e);
  }
});

// ---------------------------------------------------------------------------
// Model Performance section
// ---------------------------------------------------------------------------
async function loadMonitorStats() {
  try {
    const hours = document.getElementById('perf-range').value;
    const res = await fetch('/api/monitor-stats?hours=' + hours);
    const d = await res.json();

    const rangeLabel = {'0.167':'10m','0.5':'30m','1':'1h','12':'12h','24':'24h','168':'1w'}[hours] || hours+'h';
    document.getElementById('perf-period').textContent = '(' + rangeLabel + ', ' + d.total + ' frames)';

    // Alignment (the key metric — birdeye vs ground truth)
    const agEl = document.getElementById('perf-agreement');
    if (d.shadow && d.shadow.total > 0) {
      const pct = Math.round(d.shadow.agreementRate * 100);
      agEl.textContent = pct + '% (' + d.shadow.agreed + '/' + d.shadow.total + ')';
      agEl.style.color = pct >= 95 ? 'var(--accent-green)' : pct >= 80 ? 'var(--accent-orange)' : 'var(--accent-red)';
    } else {
      agEl.textContent = 'No data';
      agEl.style.color = '';
    }

    // Prod cost
    document.getElementById('perf-cloud-cost').textContent = d.cost
      ? '$' + d.cost.estCost.toFixed(2) + ' (' + d.cost.apiCalls + ' calls)'
      : '--';

    // Shadow latency
    document.getElementById('perf-latency').textContent =
      d.timing ? Math.round(d.timing.avg * 1000) + 'ms' : '--';

    // (Misaligned and Eye Confidence tiles removed — replaced by the
    // Safety panel which shows per-class confusion + safety metrics.)

    // Corrections pending
    document.getElementById('perf-corrections').textContent =
      trainingData ? trainingData.pendingCorrections : '--';

    // Gaps
    document.getElementById('perf-gaps').textContent = d.gaps != null ? d.gaps : '--';

    // Breakdown bar: prod pipeline (pixel-diff + cloud) with shadow overlay
    const breakdown = document.getElementById('perf-breakdown');
    if (d.total > 0) {
      const cPct = Math.round((d.methods.cloud_api || 0) / d.total * 100);
      const pPct = Math.round((d.methods.pixel_diff || 0) / d.total * 100);
      const shadowTotal = d.shadow ? d.shadow.total : 0;
      const agPct = d.shadow && shadowTotal > 0 ? Math.round(d.shadow.agreed / shadowTotal * 100) : 0;
      const dgPct = d.shadow && shadowTotal > 0 ? Math.round(d.shadow.disagreed / shadowTotal * 100) : 0;

      breakdown.innerHTML =
        '<div style="margin-bottom:6px;font-size:0.75rem;color:var(--text-dim)">Production pipeline</div>' +
        '<div class="perf-bar">' +
          (pPct > 0 ? '<div class="perf-bar-seg pixel-diff" style="width:' + pPct + '%" title="Pixel-diff ' + pPct + '%">' + (pPct > 5 ? pPct + '%' : '') + '</div>' : '') +
          (cPct > 0 ? '<div class="perf-bar-seg cloud" style="width:' + cPct + '%" title="Cloud API ' + cPct + '%">' + (cPct > 5 ? cPct + '%' : '') + '</div>' : '') +
        '</div>' +
        (shadowTotal > 0 ?
          '<div style="margin:8px 0 6px;font-size:0.75rem;color:var(--text-dim)">Shadow birdeye (' + shadowTotal + ' frames compared)</div>' +
          '<div class="perf-bar">' +
            '<div class="perf-bar-seg birdeye" style="width:' + agPct + '%" title="Aligned ' + agPct + '%">' + (agPct > 5 ? agPct + '% aligned' : '') + '</div>' +
            '<div class="perf-bar-seg spot-check" style="width:' + dgPct + '%" title="Misaligned ' + dgPct + '%">' + (dgPct > 3 ? dgPct + '%' : '') + '</div>' +
          '</div>'
          : '') +
        '<div class="perf-bar-legend">' +
          '<span><span class="legend-dot" style="background:var(--accent-blue)"></span> Pixel-diff</span>' +
          '<span><span class="legend-dot" style="background:var(--accent-orange)"></span> Cloud API</span>' +
          (shadowTotal > 0 ? '<span><span class="legend-dot" style="background:var(--accent-green)"></span> Aligned</span>' : '') +
          (shadowTotal > 0 ? '<span><span class="legend-dot" style="background:var(--accent-red)"></span> Misaligned</span>' : '') +
        '</div>';
    }
  } catch (e) {
    console.error('Monitor stats error:', e);
  }
}

// ---------------------------------------------------------------------------
// Next frame countdown timer (ticks every second)
// ---------------------------------------------------------------------------
let lastCaptureAgoSec = null;  // seconds since last capture (from API)
let lastCaptureCheckedAt = null; // Date.now() when we got that value
const CAPTURE_INTERVAL_SEC = 60;
const REFRESH_INTERVAL_SEC = 60;

let refreshTriggered = false;

function updateCountdown() {
  const el = document.getElementById('live-frame-countdown');
  if (!el || lastCaptureAgoSec == null) return;

  const elapsed = lastCaptureAgoSec + Math.floor((Date.now() - lastCaptureCheckedAt) / 1000);
  const untilCapture = Math.max(0, CAPTURE_INTERVAL_SEC - elapsed);
  const untilRefresh = untilCapture + 5; // ~5s for capture + birdeye + write

  if (untilCapture <= 0) {
    // Auto-refresh once when timer expires
    if (!refreshTriggered) {
      refreshTriggered = true;
      el.textContent = 'Refreshing...';
      setTimeout(async () => {
        await loadAll();
        // Reset countdown from now — don't get stuck if capture was late
        lastCaptureCheckedAt = Date.now();
        lastCaptureAgoSec = 0;
        refreshTriggered = false;
      }, 5000);
    }
  } else {
    el.textContent = 'Next frame in ~' + untilRefresh + 's';
  }
}

setInterval(updateCountdown, 1000);

// ---------------------------------------------------------------------------
// Safety panel: BIRDEYE-vs-ground-truth, per classifier
// ---------------------------------------------------------------------------

// Display labels for the various class names returned by /api/safety-stats.
const SAFETY_CLASS_LABELS = {
  not_present: 'not present',
  present: 'present',
  asleep: 'Asleep',
  awake: 'Awake',
  eyes_open: 'eyes_open',
  eyes_closed: 'eyes_closed',
  face_not_visible: 'face_not_visible',
};

function _safetyClass(value, thresholds) {
  // thresholds: [warn, good]. Above good = good, between = warn, below warn = bad.
  if (value == null || isNaN(value)) return '';
  if (value >= thresholds[1]) return 'good';
  if (value >= thresholds[0]) return 'warn';
  return 'bad';
}

function _renderConfusion(panel, classes) {
  if (!panel || !panel.confusion || panel.total === 0) {
    return '<div class="safety-empty">No samples in window.</div>';
  }
  const cm = panel.confusion;

  let html = '<table class="safety-cm"><thead><tr>';
  html += '<th></th>';
  for (const c of classes) {
    html += '<th>' + (SAFETY_CLASS_LABELS[c] || c) + '</th>';
  }
  html += '<th>n</th></tr></thead><tbody>';

  for (const truth of classes) {
    const row = cm[truth] || {};
    const rowTotal = classes.reduce((a, c) => a + (row[c] || 0), 0);
    html += '<tr><td class="cm-row-label">' + (SAFETY_CLASS_LABELS[truth] || truth) + '</td>';
    for (const pred of classes) {
      const v = row[pred] || 0;
      let cls;
      if (v === 0) {
        cls = 'cm-zero';
      } else if (truth === pred) {
        cls = 'cm-correct';
      } else {
        const ratio = rowTotal > 0 ? v / rowTotal : 0;
        if (ratio < 0.1) cls = 'cm-error-low';
        else if (ratio < 0.3) cls = 'cm-error-mid';
        else cls = 'cm-error-high';
      }
      html += '<td class="' + cls + '">' + v + '</td>';
    }
    html += '<td class="cm-row-label">' + rowTotal + '</td></tr>';
  }
  html += '</tbody></table>';
  return html;
}

function _renderPerClass(panel, classes) {
  if (!panel || !panel.perClass || panel.total === 0) return '';
  let html = '<table class="safety-pc"><thead><tr>';
  html += '<th>class</th><th>P</th><th>R</th><th>F1</th><th>n</th>';
  html += '</tr></thead><tbody>';
  for (const c of classes) {
    const m = panel.perClass[c];
    if (!m) continue;
    html += '<tr><td>' + (SAFETY_CLASS_LABELS[c] || c) + '</td>';
    html += '<td>' + (m.precision * 100).toFixed(0) + '%</td>';
    html += '<td>' + (m.recall * 100).toFixed(0) + '%</td>';
    html += '<td>' + (m.f1 * 100).toFixed(0) + '%</td>';
    html += '<td>' + m.support + '</td></tr>';
  }
  html += '</tbody></table>';
  return html;
}

function _renderCorrectionsByClass(byClass, classOrder) {
  // Renders a small table for the corrections-side per-class breakdown.
  if (!byClass) return '';
  let html = '<table class="safety-pc"><thead><tr>';
  html += '<th>class</th><th>correct</th><th>n</th><th>%</th>';
  html += '</tr></thead><tbody>';
  for (const c of classOrder) {
    const v = byClass[c];
    if (!v || v.total === 0) continue;
    const pct = Math.round((v.correct / v.total) * 100);
    html += '<tr><td>' + (SAFETY_CLASS_LABELS[c] || c) + '</td>';
    html += '<td>' + v.correct + '</td>';
    html += '<td>' + v.total + '</td>';
    html += '<td>' + pct + '%</td></tr>';
  }
  html += '</tbody></table>';
  return html;
}

function renderEyeStateColumn(eyeState) {
  const cls = ['eyes_open', 'eyes_closed', 'face_not_visible'];
  const correctionsCls = ['eyes_open', 'eyes_closed', 'face_not_visible'];

  // Headline: macro F1 + accuracy (raw classifier quality — no derived
  // Awake/Asleep concept, which requires more logic and is decoupled).
  const cloud = eyeState.vsCloud || {};
  const macroF1 = cloud.macroF1;
  const accuracy = cloud.accuracy;

  const macroClass = _safetyClass(macroF1, [0.60, 0.85]);
  const accClass = _safetyClass(accuracy, [0.75, 0.90]);

  let html = '<h3>Eye State</h3>';
  html += '<div class="safety-headline">';
  html += '<div class="safety-headline-row" title="Macro-averaged F1 across {eyes_open, eyes_closed, face_not_visible}. Equal weight per class — doesn\'t get fooled by class imbalance.">';
  html +=   '<span class="safety-headline-label">Macro F1</span>';
  html +=   '<span class="safety-headline-value ' + macroClass + '">' + (macroF1 != null ? (macroF1 * 100).toFixed(0) + '%' : '--') + '</span>';
  html += '</div>';
  html += '<div class="safety-headline-row" title="Overall fraction of frames where birdeye\'s eye-state prediction matches the cloud API. Biased toward the majority class — see Macro F1 and the per-class breakdown below.">';
  html +=   '<span class="safety-headline-label">Accuracy</span>';
  html +=   '<span class="safety-headline-value ' + accClass + '">' + (accuracy != null ? Math.round(accuracy * 100) + '%' : '--') + '</span>';
  html += '</div>';
  html += '</div>';

  // Vs cloud
  html += '<div class="safety-source-label">vs Cloud API</div>';
  html += _renderConfusion(eyeState.vsCloud, cls);
  html += _renderPerClass(eyeState.vsCloud, cls);

  // Vs corrections
  html += '<div class="safety-source-label">vs Corrections</div>';
  if (eyeState.vsCorrections && eyeState.vsCorrections.total > 0) {
    const c = eyeState.vsCorrections;
    html += '<div class="safety-headline-row" style="margin-bottom:4px">';
    html +=   '<span class="safety-headline-label">Accuracy</span>';
    html +=   '<span class="safety-headline-value ' + _safetyClass(c.accuracy, [0.75, 0.90]) + '">'
            + Math.round(c.accuracy * 100) + '% (' + c.total + ' samples)</span>';
    html += '</div>';
    html += _renderCorrectionsByClass(c.byClass, correctionsCls);
  } else {
    html += '<div class="safety-empty">No data yet — populates on next retrain.</div>';
  }
  return html;
}

function renderPresenceColumn(presence) {
  const cls = ['not_present', 'present'];
  const correctionsCls = ['not_present', 'present'];

  const acc = presence.vsCloud ? presence.vsCloud.accuracy : null;
  const macroF1 = presence.vsCloud ? presence.vsCloud.macroF1 : null;

  const accClass = _safetyClass(acc, [0.90, 0.97]);
  const macroClass = _safetyClass(macroF1, [0.90, 0.97]);

  let html = '<h3>Presence</h3>';
  html += '<div class="safety-headline">';
  html += '<div class="safety-headline-row" title="Overall fraction of frames where birdeye and cloud API agree on present vs not_present.">';
  html +=   '<span class="safety-headline-label">Accuracy</span>';
  html +=   '<span class="safety-headline-value ' + accClass + '">' + (acc != null ? Math.round(acc * 100) + '%' : '--') + '</span>';
  html += '</div>';
  html += '<div class="safety-headline-row" title="Macro-averaged F1 across {not_present, present}. Equal weight per class.">';
  html +=   '<span class="safety-headline-label">Macro F1</span>';
  html +=   '<span class="safety-headline-value ' + macroClass + '">' + (macroF1 != null ? (macroF1 * 100).toFixed(0) + '%' : '--') + '</span>';
  html += '</div>';
  html += '</div>';

  // Vs cloud
  html += '<div class="safety-source-label">vs Cloud API</div>';
  html += _renderConfusion(presence.vsCloud, cls);
  html += _renderPerClass(presence.vsCloud, cls);

  // Vs corrections
  html += '<div class="safety-source-label">vs Corrections</div>';
  if (presence.vsCorrections && presence.vsCorrections.total > 0) {
    const c = presence.vsCorrections;
    html += '<div class="safety-headline-row" style="margin-bottom:4px">';
    html +=   '<span class="safety-headline-label">Accuracy</span>';
    html +=   '<span class="safety-headline-value ' + _safetyClass(c.accuracy, [0.90, 0.97]) + '">'
            + Math.round(c.accuracy * 100) + '% (' + c.total + ' samples)</span>';
    html += '</div>';
    html += _renderCorrectionsByClass(c.byClass, correctionsCls);
  } else {
    html += '<div class="safety-empty">No data yet — populates on next retrain.</div>';
  }
  return html;
}

async function loadSafetyStats() {
  try {
    const hours = document.getElementById('safety-range').value;
    const res = await fetch('/api/safety-stats?hours=' + hours);
    const d = await res.json();

    const rangeLabel = hours === '24' ? '24h' : '7d';
    const total = d.shadowTotal || 0;
    document.getElementById('safety-period').textContent = '(' + rangeLabel + ', ' + total + ' shadow frames)';
    document.getElementById('safety-deployed').textContent = d.deployedVersion ? '— ' + d.deployedVersion : '';

    const rollbackBadge = document.getElementById('safety-rollback-badge');
    if (d.rolledBack) {
      rollbackBadge.textContent = '⚠ rolled back from ' + d.latestTrainedVersion;
      rollbackBadge.style.display = '';
    } else {
      rollbackBadge.style.display = 'none';
    }

    document.getElementById('safety-eye').innerHTML = renderEyeStateColumn(d.eyeState || {});
    document.getElementById('safety-presence').innerHTML = renderPresenceColumn(d.presence || {});
  } catch (e) {
    console.error('Safety stats error:', e);
  }
}

// ---------------------------------------------------------------------------
// Init & auto-refresh
// ---------------------------------------------------------------------------
async function loadAll() {
  await Promise.all([
    loadStatus(),
    loadTimeline(),
    loadEvents(),
    loadTrainingStatus(),
    loadMonitorStats(),
    loadSafetyStats(),
  ]);
  document.getElementById('footer-refresh').textContent =
    'Last refreshed: ' + new Date().toLocaleTimeString('en-US', { timeZone: 'America/New_York' });
}

initTimelineNav();
document.getElementById('perf-range').addEventListener('change', loadMonitorStats);
document.getElementById('safety-range').addEventListener('change', loadSafetyStats);
document.getElementById('events-count').addEventListener('change', loadEvents);
loadAll();
setInterval(loadAll, REFRESH_INTERVAL_SEC * 1000);
