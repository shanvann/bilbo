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
      // no timeline entries
      return;
    }

    // Timeline stats now handled by bassinet chart

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

    // Merge consecutive entries with same state category into blocks.
    // Asleep / Unknown / Awake are rendered as distinct blocks — no more
    // Asleep+Unknown collapsing. Unknown means the temporal smoother
    // couldn't confirm a state from the last 6 frames, and is worth
    // seeing on the timeline instead of being silently shown as Asleep.
    function stateCategory(e) {
      if (!e.babyPresent) return 'absent';
      if (e.state === 'Awake') return 'awake';
      if (e.state === 'Asleep') return 'asleep';
      return 'unknown-present';
    }

    function stateLabel(e) {
      if (!e.babyPresent) return 'Out of bassinet';
      if (e.state === 'Asleep') return 'Asleep';
      if (e.state === 'Awake') return 'Awake';
      return 'Unknown (in bassinet)';
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
          label: stateLabel(e),
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
let safetyData = null;   // shared safety stats from API
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

  // Reviewed checkbox: checked if ALL frames in block are reviewed
  const allReviewed = seg.entries.length > 0 && seg.entries.every(e => e.reviewed);
  const reviewedCb = document.getElementById('block-reviewed');
  reviewedCb.checked = allReviewed;
  reviewedCb.disabled = allReviewed; // can't un-review
  document.getElementById('block-reviewed-status').textContent = '';

  panel.style.display = 'block';
  panel.scrollIntoView({ behavior: 'smooth' });
}

// ---------------------------------------------------------------------------
// Face bbox overlay + drag-to-draw correction
// ---------------------------------------------------------------------------

// BASSINET_CROP must match config.py for coordinate mapping
const BASSINET_CROP = { x: 0.15, y: 0.10, w: 0.70, h: 0.80 };

function _getImageLayout(img) {
  // Compute object-fit:contain scaling and offset
  const natW = img.naturalWidth, natH = img.naturalHeight;
  const dispW = img.clientWidth, dispH = img.clientHeight;
  if (!natW || !natH || !dispW || !dispH) return null;
  const scale = Math.min(dispW / natW, dispH / natH);
  return {
    scale, natW, natH,
    renderedW: natW * scale, renderedH: natH * scale,
    offsetX: (dispW - natW * scale) / 2,
    offsetY: (dispH - natH * scale) / 2,
  };
}

function _faceBboxToPixels(bbox, layout) {
  // Convert normalized faceBbox (relative to bassinet crop) to rendered pixel coords.
  // faceBbox is {x1,y1,x2,y2} as fractions of the bassinet crop.
  // The full image includes non-bassinet areas, so we map:
  //   image_x = BASSINET_CROP.x + bbox.x1 * BASSINET_CROP.w
  const bc = BASSINET_CROP;
  const imgX1 = (bc.x + bbox.x1 * bc.w) * layout.natW;
  const imgY1 = (bc.y + bbox.y1 * bc.h) * layout.natH;
  const imgX2 = (bc.x + bbox.x2 * bc.w) * layout.natW;
  const imgY2 = (bc.y + bbox.y2 * bc.h) * layout.natH;
  return {
    left: layout.offsetX + imgX1 * layout.scale,
    top: layout.offsetY + imgY1 * layout.scale,
    width: (imgX2 - imgX1) * layout.scale,
    height: (imgY2 - imgY1) * layout.scale,
  };
}

function drawFaceOverlay(img, overlay, entry) {
  const bbox = entry.faceBboxCorrected || entry.faceBbox;
  if (!bbox || bbox.x1 == null) {
    overlay.style.display = 'none';
    return;
  }
  const layout = _getImageLayout(img);
  if (!layout) { overlay.style.display = 'none'; return; }

  const px = _faceBboxToPixels(bbox, layout);
  const isCorrected = !!entry.faceBboxCorrected;

  overlay.style.display = 'block';
  overlay.style.left = px.left + 'px';
  overlay.style.top = px.top + 'px';
  overlay.style.width = px.width + 'px';
  overlay.style.height = px.height + 'px';
  overlay.className = 'face-overlay' + (isCorrected ? ' corrected' : '');
  overlay.innerHTML = '<span class="face-overlay-label">' + (isCorrected ? 'corrected' : 'auto') + '</span>';
}

// Drag-to-draw state
let _drawing = false;
let _drawStart = null;

function _initDragToDraw() {
  const container = document.getElementById('viewer-frame-container');
  const drawRect = document.getElementById('viewer-draw-rect');
  const img = document.getElementById('viewer-img');

  container.addEventListener('mousedown', (ev) => {
    if (ev.target !== img && ev.target !== container) return;
    ev.preventDefault();
    const rect = container.getBoundingClientRect();
    _drawing = true;
    _drawStart = { x: ev.clientX - rect.left, y: ev.clientY - rect.top };
    drawRect.style.display = 'block';
    drawRect.style.left = _drawStart.x + 'px';
    drawRect.style.top = _drawStart.y + 'px';
    drawRect.style.width = '0px';
    drawRect.style.height = '0px';
  });

  container.addEventListener('mousemove', (ev) => {
    if (!_drawing) return;
    const rect = container.getBoundingClientRect();
    const cx = ev.clientX - rect.left;
    const cy = ev.clientY - rect.top;
    const x = Math.min(_drawStart.x, cx);
    const y = Math.min(_drawStart.y, cy);
    const w = Math.abs(cx - _drawStart.x);
    const h = Math.abs(cy - _drawStart.y);
    drawRect.style.left = x + 'px';
    drawRect.style.top = y + 'px';
    drawRect.style.width = w + 'px';
    drawRect.style.height = h + 'px';
  });

  container.addEventListener('mouseup', async (ev) => {
    if (!_drawing) return;
    _drawing = false;
    drawRect.style.display = 'none';

    const rect = container.getBoundingClientRect();
    const cx = ev.clientX - rect.left;
    const cy = ev.clientY - rect.top;

    // Min size check (at least 10px)
    if (Math.abs(cx - _drawStart.x) < 10 || Math.abs(cy - _drawStart.y) < 10) return;

    const layout = _getImageLayout(img);
    if (!layout) return;

    // Convert rendered pixel coords back to normalized face bbox (relative to bassinet crop)
    const bc = BASSINET_CROP;
    const px1 = Math.min(_drawStart.x, cx);
    const py1 = Math.min(_drawStart.y, cy);
    const px2 = Math.max(_drawStart.x, cx);
    const py2 = Math.max(_drawStart.y, cy);

    // Rendered pixels → full image normalized coords
    const imgNx1 = (px1 - layout.offsetX) / layout.scale / layout.natW;
    const imgNy1 = (py1 - layout.offsetY) / layout.scale / layout.natH;
    const imgNx2 = (px2 - layout.offsetX) / layout.scale / layout.natW;
    const imgNy2 = (py2 - layout.offsetY) / layout.scale / layout.natH;

    // Full image normalized → bassinet crop normalized
    const bx1 = Math.max(0, Math.min(1, (imgNx1 - bc.x) / bc.w));
    const by1 = Math.max(0, Math.min(1, (imgNy1 - bc.y) / bc.h));
    const bx2 = Math.max(0, Math.min(1, (imgNx2 - bc.x) / bc.w));
    const by2 = Math.max(0, Math.min(1, (imgNy2 - bc.y) / bc.h));

    const faceBbox = {
      x1: Math.round(bx1 * 10000) / 10000,
      y1: Math.round(by1 * 10000) / 10000,
      x2: Math.round(bx2 * 10000) / 10000,
      y2: Math.round(by2 * 10000) / 10000,
    };

    // Save to backend
    const e = viewerEntries[viewerIndex];
    if (!e) return;
    const saved = document.getElementById('viewer-saved');
    saved.textContent = 'saving face...';
    try {
      const res = await fetch('/api/update-entry', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ timestamp: e.timestamp, faceBbox: faceBbox }),
      });
      const data = await res.json();
      if (data.ok) {
        e.faceBboxCorrected = faceBbox;
        saved.textContent = 'face saved';
        saved.style.color = '#4caf50';
        setTimeout(() => { saved.textContent = ''; renderViewer(); }, 800);
      } else {
        saved.textContent = 'error';
        saved.style.color = '#ff5252';
      }
    } catch (err) {
      saved.textContent = 'error';
      saved.style.color = '#ff5252';
    }
  });
}

// Clear face button
document.getElementById('viewer-clear-face').addEventListener('click', async () => {
  const e = viewerEntries[viewerIndex];
  if (!e) return;
  const saved = document.getElementById('viewer-saved');
  saved.textContent = 'clearing...';
  try {
    const res = await fetch('/api/update-entry', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ timestamp: e.timestamp, faceBbox: false }),
    });
    const data = await res.json();
    if (data.ok) {
      e.faceBboxCorrected = null;
      saved.textContent = 'cleared';
      saved.style.color = '#4a9eff';
      setTimeout(() => { saved.textContent = ''; renderViewer(); }, 800);
    }
  } catch (err) {
    saved.textContent = 'error';
    saved.style.color = '#ff5252';
  }
});

_initDragToDraw();

// Run Inference button — re-run BIRDEYE on current frame
document.getElementById('viewer-run-inference').addEventListener('click', async () => {
  const e = viewerEntries[viewerIndex];
  if (!e) return;
  const saved = document.getElementById('viewer-saved');
  const btn = document.getElementById('viewer-run-inference');
  btn.disabled = true;
  btn.textContent = 'Running...';
  saved.textContent = '';

  try {
    const res = await fetch('/api/run-inference', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ timestamp: e.timestamp }),
    });
    const data = await res.json();
    if (data.ok && data.shadow) {
      // Update local entry with new shadow data
      e.shadowBirdeyeState = data.shadow.birdeyeState;
      e.shadowEyeState = data.shadow.eyeState;
      e.shadowPresenceConfidence = data.shadow.presenceConfidence;
      e.shadowEyeConfidence = data.shadow.eyeConfidence;
      e.shadowFallback = data.shadow.fallback;
      if (data.faceBbox) e.faceBbox = data.faceBbox;
      if (data.faceConfidence != null) e.faceConfidence = data.faceConfidence;
      if (data.retrainAgreed != null) e.retrainAgreed = data.retrainAgreed;
      saved.textContent = 'inference done';
      saved.style.color = '#4caf50';
      renderViewer();
    } else {
      saved.textContent = data.reason || 'no result';
      saved.style.color = 'var(--accent-orange)';
    }
  } catch (err) {
    saved.textContent = 'error';
    saved.style.color = '#ff5252';
    console.error('Inference error:', err);
  }

  btn.disabled = false;
  btn.textContent = 'Run Inference';
  setTimeout(() => { saved.textContent = ''; }, 3000);
});

function renderViewer() {
  if (viewerEntries.length === 0) return;
  const e = viewerEntries[viewerIndex];

  // Image
  const img = document.getElementById('viewer-img');
  const faceOverlay = document.getElementById('viewer-face-overlay');
  if (e.frame) {
    img.src = frameUrl(e.frame);
    img.style.display = 'block';
    img.onclick = () => showFrameModal(e.frame);
    img.onload = function() { drawFaceOverlay(img, faceOverlay, e); };
  } else {
    img.style.display = 'none';
    faceOverlay.style.display = 'none';
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
  const eyeState = e.eyeState || (!e.babyPresent ? 'not_in_bassinet' : e.state === 'Awake' ? 'eyes_open' : 'eyes_closed');
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
  // Face detection label
  const faceEl = document.getElementById('viewer-birdeye-face');
  if (e.faceBbox || e.faceBboxCorrected) {
    const fc = e.faceConfidence;
    const corrected = !!e.faceBboxCorrected;
    let faceText = 'BIRDEYE face: detected' + (fc != null ? ' (' + Math.round(fc * 100) + '%)' : '');
    if (corrected) faceText += ' [corrected]';
    faceEl.textContent = faceText;
    faceEl.style.display = '';
    faceEl.classList.remove('disagree');
  } else if (e.shadowFallback === 'no_face_detected') {
    faceEl.textContent = 'BIRDEYE face: not detected → fallback';
    faceEl.style.display = '';
    faceEl.classList.add('disagree');
  } else if (e.shadowBirdeyeState && e.shadowBirdeyeState !== 'not_present') {
    faceEl.textContent = 'BIRDEYE face: —';
    faceEl.style.display = '';
    faceEl.classList.remove('disagree');
  } else {
    faceEl.style.display = 'none';
  }

  const EYE_CONF_THRESHOLD = 0.7; // must match EYE_STATE_CONFIDENCE_THRESHOLD in config.py
  if (e.shadowEyeState != null) {
    const lowConf = e.shadowEyeConfidence != null && e.shadowEyeConfidence < EYE_CONF_THRESHOLD;
    let eyeText = 'BIRDEYE eyes: ' + (labelMap[e.shadowEyeState] || e.shadowEyeState) + fmtConf(e.shadowEyeConfidence);
    if (lowConf) eyeText += ' ⚠ low conf → cloud fallback';
    eyeEl.textContent = eyeText;
    eyeEl.style.display = '';
    const agreedEye = e.shadowEyeState === eyeState;
    eyeEl.classList.toggle('disagree', !agreedEye || lowConf);
  } else if (e.shadowBirdeyeState === 'not_present') {
    // Eye classifier skipped (no baby present)
    eyeEl.textContent = 'BIRDEYE eyes: — (skipped)';
    eyeEl.style.display = '';
    eyeEl.classList.remove('disagree');
  } else if (e.shadowBirdeyeState != null) {
    // Present but no eye state — show specific fallback reason
    const fallback = e.shadowFallback;
    const reason = fallback === 'no_face_detected' ? 'no face detected'
      : fallback === 'low_confidence' ? 'low confidence'
      : 'unknown';
    eyeEl.textContent = 'BIRDEYE eyes: — (' + reason + ' → cloud fallback)';
    eyeEl.style.display = '';
    eyeEl.classList.add('disagree');
  } else {
    eyeEl.style.display = 'none';
    eyeEl.classList.remove('disagree');
  }

  // --- Shadow-experiment labels ---
  // One label per registered shadow experiment with a result on this
  // frame. Each label is a direct child of .viewer-meta (not wrapped in
  // a container) so the flex layout treats it as its own flex item,
  // matching the existing BIRDEYE labels. Compared against prod's
  // eyeState (= the user-facing label the entry currently shows) to
  // flag disagreement — same semantics as the BIRDEYE labels above.
  //
  // Previously-injected experiment spans are stripped on every render
  // before we insert fresh ones, so scrolling through the viewer can't
  // accumulate stale labels from a prior frame.
  const metaContainer = document.querySelector('#block-detail-viewer .viewer-meta');
  if (metaContainer) {
    metaContainer.querySelectorAll('.viewer-experiment-label').forEach((n) => n.remove());
  }
  const marker = document.getElementById('viewer-experiments-marker');
  const expDict = e.experiments;
  if (metaContainer && marker && expDict && typeof expDict === 'object') {
    for (const name of Object.keys(expDict)) {
      const r = expDict[name] || {};
      const rEye = r.eyeState;
      if (!rEye) continue;
      const lbl = labelMap[rEye] || rEye;
      const conf = r.eyeConfidence != null ? ' (' + Math.round(r.eyeConfidence * 100) + '%)' : '';
      const ver = r.modelVersion ? ' · ' + r.modelVersion : '';
      const lat = r.latencyMs != null ? ' · ' + Math.round(r.latencyMs) + 'ms' : '';
      // Disagrees with prod eyeState → mark red. Same semantics as the
      // BIRDEYE eye label's .disagree class.
      const disagree = rEye !== eyeState;
      // Abbreviate long experiment names so the row stays readable —
      // the tooltip carries the full name + model version + latency.
      const shortName = name.length > 28 ? name.slice(0, 26) + '…' : name;

      const span = document.createElement('span');
      span.className = 'viewer-birdeye-label viewer-experiment-label' + (disagree ? ' disagree' : '');
      span.title = name + ver + lat;
      span.textContent = 'shadow(' + shortName + '): ' + lbl + conf;
      marker.parentNode.insertBefore(span, marker.nextSibling);
    }
  }

  // Eye state dropdown
  const stateSelect = document.getElementById('viewer-state');
  stateSelect.value = eyeState;

  // Retrain status indicator (derived from training API data + retrainAgreed)
  const retrainEl = document.getElementById('viewer-retrain-status');
  const correctedAtStr = e.eyeStateCorrectedAt || e._correctedAt;
  const lastTrainedStr = trainingData && trainingData.lastTrained;
  if (e.eyeStateEdited || correctedAtStr) {
    const correctedAt = correctedAtStr ? new Date(correctedAtStr) : null;
    const lastTrained = lastTrainedStr ? new Date(lastTrainedStr) : null;
    if (lastTrained && correctedAt && correctedAt < lastTrained) {
      // Was retrained — show whether inference now agrees with correction
      if (e.retrainAgreed === true) {
        retrainEl.textContent = 'retrained ✓';
        retrainEl.className = 'viewer-retrain-status retrained';
      } else if (e.retrainAgreed === false) {
        retrainEl.textContent = 'retrained ✗ still disagrees';
        retrainEl.className = 'viewer-retrain-status retrain-disagree';
      } else {
        retrainEl.textContent = 'retrained';
        retrainEl.className = 'viewer-retrain-status retrained';
      }
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

// Save eye state label for current frame
async function saveEyeState(newEyeState) {
  const e = viewerEntries[viewerIndex];
  if (!e) return;
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
}

// Eye state change from dropdown
document.getElementById('viewer-state').addEventListener('change', (ev) => {
  saveEyeState(ev.target.value);
});

// Confirm button — saves current dropdown value even if unchanged
document.getElementById('viewer-confirm').addEventListener('click', () => {
  saveEyeState(document.getElementById('viewer-state').value);
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
// Reviewed checkbox — marks all frames in the block as reviewed ground truth
document.getElementById('block-reviewed').addEventListener('change', async (ev) => {
  if (!ev.target.checked) return; // can't un-review
  if (viewerEntries.length === 0) return;

  const status = document.getElementById('block-reviewed-status');
  status.textContent = 'saving...';
  status.style.color = 'var(--text-dim)';

  const timestamps = viewerEntries.map(e => e.timestamp);
  try {
    const res = await fetch('/api/mark-reviewed', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ timestamps }),
    });
    const data = await res.json();
    if (data.ok) {
      viewerEntries.forEach(e => { e.reviewed = true; });
      status.textContent = data.updated + ' frames reviewed';
      status.style.color = 'var(--accent-green)';
      setTimeout(() => { status.textContent = ''; }, 3000);
    } else {
      status.textContent = 'error';
      status.style.color = '#ff5252';
    }
  } catch (err) {
    status.textContent = 'error';
    status.style.color = '#ff5252';
  }
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
// ---------------------------------------------------------------------------
// Daily bassinet chart
// ---------------------------------------------------------------------------
async function loadBassinetChart() {
  try {
    const days = document.getElementById('bassinet-days').value;
    const res = await fetch('/api/bassinet-daily?days=' + days);
    const data = await res.json();
    const chartEl = document.getElementById('bassinet-chart');

    if (!data.days || data.days.length === 0) {
      chartEl.innerHTML = '<div style="color:var(--text-dim);padding:20px;text-align:center">No data</div>';
      return;
    }

    // Find max total hours for scaling
    const maxHours = Math.max(...data.days.map(d => d.inHours + d.outHours), 1);

    // Stack segments bottom → top: out, awake, unknown-in, asleep.
    // Asleep sits at the top so the "good sleep" color is the most
    // visually prominent slice. Tiny slices (< 0.1h) are rolled into
    // the title tooltip only so they don't create a 1px stripe.
    //
    // Bars sit inside a horizontal row wrapper so the legend (appended
    // after it) stacks below the bars rather than becoming a flex
    // sibling to the right.
    let html = '<div class="bassinet-bars-row">';
    for (const d of data.days) {
      const total = d.inHours + d.outHours;
      const stackPct = total > 0 ? (total / maxHours * 100) : 0;

      function fmtHrs(h) { return h >= 1 ? h + 'h' : ''; }
      function segFlex(h) { return total > 0 ? stackPct * (h / total) : 0; }

      const dt = new Date(d.date + 'T12:00:00');
      const dayName = dt.toLocaleDateString('en-US', { weekday: 'short' });
      const monthDay = (dt.getMonth() + 1) + '/' + dt.getDate();

      html += '<div class="bassinet-bar-group">';
      html += '<div class="bassinet-bar-stack" style="height:100%">';
      html += '<div style="flex:' + (100 - stackPct) + '"></div>'; // spacer

      if (d.outHours > 0) {
        html += '<div class="bassinet-bar-seg out" style="flex:' + segFlex(d.outHours)
          + '" title="Out of bassinet: ' + d.outHours + 'h">' + fmtHrs(d.outHours) + '</div>';
      }
      if (d.awakeHours > 0) {
        html += '<div class="bassinet-bar-seg awake" style="flex:' + segFlex(d.awakeHours)
          + '" title="Awake in bassinet: ' + d.awakeHours + 'h">' + fmtHrs(d.awakeHours) + '</div>';
      }
      if (d.unknownInHours > 0) {
        html += '<div class="bassinet-bar-seg unknown-in" style="flex:' + segFlex(d.unknownInHours)
          + '" title="Unknown (in bassinet): ' + d.unknownInHours + 'h">' + fmtHrs(d.unknownInHours) + '</div>';
      }
      if (d.asleepHours > 0) {
        html += '<div class="bassinet-bar-seg asleep" style="flex:' + segFlex(d.asleepHours)
          + '" title="Asleep: ' + d.asleepHours + 'h">' + fmtHrs(d.asleepHours) + '</div>';
      }

      html += '</div>';
      html += '<div class="bassinet-bar-label">' + dayName + '<br>' + monthDay + '</div>';
      html += '</div>';
    }

    html += '</div>';
    // Legend
    html += '<div class="bassinet-chart-legend">';
    html += '<span><span class="legend-dot" style="background:rgba(74,158,255,0.8)"></span> Asleep</span>';
    html += '<span><span class="legend-dot" style="background:rgba(74,158,255,0.18)"></span> Unknown (in bassinet)</span>';
    html += '<span><span class="legend-dot" style="background:rgba(74,158,255,0.35)"></span> Awake</span>';
    html += '<span><span class="legend-dot" style="background:rgba(255,152,0,0.5)"></span> Out of bassinet</span>';
    html += '</div>';

    chartEl.innerHTML = html;
  } catch (e) {
    console.error('Bassinet chart error:', e);
  }
}


// ---------------------------------------------------------------------------
// Pending Corrections
// ---------------------------------------------------------------------------

// Inline label editor rendered in every "Corrected To" cell — users can
// re-edit a previously-saved label (mistakes happen), and the row's
// `corrected_at` bumps to now on every save so the re-edit lands ahead of
// the previous training cutoff. Originally built to rescue phantom rows
// (null corrected fields from pre-2026-04-19 bbox-only edits); now the
// same control set serves both cases, with prefill rules:
//
//   1. If a corrected eye-state already exists on the row → prefill that
//      (so the dropdown shows the label the user last saved).
//   2. Otherwise, fall back to BIRDEYE's shadow prediction as a
//      one-click "confirm what the model said" shortcut.
//   3. Otherwise leave the dropdown unselected.
//
// Discard remains available on every row: real corrections can also be
// struck (the user realised the original label was actually right).
function _buildCorrectionEditor(c) {
  const wrap = document.createElement('span');
  wrap.className = 'corr-editor-controls';

  const select = document.createElement('select');
  select.className = 'corr-editor-select';
  const options = [
    { value: '', text: '—' },
    { value: 'eyes_open', text: 'Eyes Open' },
    { value: 'eyes_closed', text: 'Eyes Closed' },
    { value: 'face_not_visible', text: 'Face Not Visible' },
    { value: 'not_in_bassinet', text: 'Not In Bassinet' },
  ];
  const existing = c.correctedEyeState || '';
  const shadowSuggestion = c.shadowBirdeyeEye
    || (c.shadowBirdeyePresent === 0 ? 'not_in_bassinet' : '');
  const prefill = existing || shadowSuggestion;
  for (const o of options) {
    const opt = document.createElement('option');
    opt.value = o.value;
    opt.textContent = o.text;
    if (o.value && o.value === prefill) opt.selected = true;
    select.appendChild(opt);
  }
  wrap.appendChild(select);

  const saveBtn = document.createElement('button');
  saveBtn.textContent = 'Save';
  saveBtn.className = 'corr-editor-btn corr-editor-save';
  saveBtn.title = 'Save label. The Corrected timestamp updates to now on every save, so re-edits land after the previous training cutoff.';
  saveBtn.onclick = async () => {
    const value = select.value;
    if (!value) { saveBtn.textContent = 'pick one'; return; }
    saveBtn.disabled = true;
    saveBtn.textContent = 'saving…';
    try {
      const res = await fetch('/api/correction/resolve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: c.id, eyeState: value }),
      });
      if (!res.ok) throw new Error('http ' + res.status);
      await loadPendingCorrections();
    } catch (err) {
      saveBtn.disabled = false;
      saveBtn.textContent = 'error';
      console.error('Resolve correction error:', err);
    }
  };
  wrap.appendChild(saveBtn);

  const discardBtn = document.createElement('button');
  discardBtn.textContent = 'Discard';
  discardBtn.className = 'corr-editor-btn corr-editor-discard';
  discardBtn.title = 'Delete this correction row. The frame\'s bbox correction (if any) is preserved — only the label-correction row is removed.';
  discardBtn.onclick = async () => {
    discardBtn.disabled = true;
    discardBtn.textContent = '…';
    try {
      const res = await fetch('/api/correction/discard', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: c.id }),
      });
      if (!res.ok) throw new Error('http ' + res.status);
      await loadPendingCorrections();
    } catch (err) {
      discardBtn.disabled = false;
      discardBtn.textContent = 'error';
      console.error('Discard correction error:', err);
    }
  };
  wrap.appendChild(discardBtn);

  return wrap;
}

async function loadPendingCorrections() {
  try {
    const res = await fetch('/api/pending-corrections');
    const data = await res.json();
    const corrections = data.corrections || [];

    const summary = document.getElementById('corrections-summary');
    const tbody = document.getElementById('corrections-body');
    const emptyEl = document.getElementById('corrections-empty');
    const tableEl = document.getElementById('corrections-table');
    const breakdownEl = document.getElementById('corrections-breakdown');

    if (corrections.length === 0) {
      summary.textContent = '';
      tbody.innerHTML = '';
      tableEl.style.display = 'none';
      breakdownEl.innerHTML = '';
      emptyEl.style.display = '';
      return;
    }

    tableEl.style.display = '';
    emptyEl.style.display = 'none';
    summary.textContent = '(' + corrections.length + ' pending)';

    // Breakdown chips
    const changes = data.eyeStateChanges || {};
    const labelMap = {
      eyes_open: 'Eyes Open', eyes_closed: 'Eyes Closed',
      face_not_visible: 'Face Not Visible', not_in_bassinet: 'Not In Bassinet',
      Awake: 'Awake', Asleep: 'Asleep', Unknown: 'Unknown',
      unknown: '?', null: '?',
    };
    function friendlyLabel(s) { return labelMap[s] || s || '?'; }
    function friendlyChange(key) {
      const parts = key.split(' → ');
      return friendlyLabel(parts[0]) + ' → ' + friendlyLabel(parts[1]);
    }

    let chips = '';
    for (const [change, count] of Object.entries(changes).sort((a, b) => b[1] - a[1])) {
      chips += '<span class="corr-chip">' + friendlyChange(change) + ' <strong>' + count + '</strong></span>';
    }
    breakdownEl.innerHTML = chips;

    // Table rows
    tbody.innerHTML = '';
    for (const c of corrections) {
      const tr = document.createElement('tr');

      // Corrected at
      const tdWhen = document.createElement('td');
      tdWhen.textContent = formatDateTimeET(c.correctedAt);
      tr.appendChild(tdWhen);

      // Frame timestamp
      const tdFrame = document.createElement('td');
      tdFrame.textContent = formatDateTimeET(c.originalTimestamp);
      tr.appendChild(tdFrame);

      // Original label (prefer eye state, fall back to sleep state)
      const tdOrig = document.createElement('td');
      tdOrig.textContent = friendlyLabel(c.originalEyeState || c.originalState);
      tdOrig.className = 'corr-label';
      tr.appendChild(tdOrig);

      // Corrected label — every row gets an inline editor so the user can
      // re-pick a label after realising the first choice was wrong. Each
      // save bumps `corrected_at` to now via /api/correction/resolve, so
      // re-edits land after the previous training cutoff. Rows with a
      // saved label prefill the dropdown from it; phantom rows (pre-
      // 2026-04-19 bbox-only edits) fall back to BIRDEYE's shadow.
      const tdCorr = document.createElement('td');
      tdCorr.className = 'corr-label corr-editor';
      if (c.id != null) {
        tdCorr.appendChild(_buildCorrectionEditor(c));
      } else {
        // Defensive: pre-migration rows without an id can't be targeted.
        tdCorr.textContent = friendlyLabel(c.correctedEyeState || c.correctedState);
      }
      tr.appendChild(tdCorr);

      // BIRDEYE prediction — eye-state direct from shadow_birdeye_eye column.
      // Colored green when it agrees with the saved correction, yellow when
      // it disagrees. Neutral (no color) when the correction has no label
      // yet: there's nothing to compare against, so colouring would imply a
      // disagreement that doesn't exist. Re-paints on every save because
      // loadPendingCorrections() rebuilds the whole row list post-resolve.
      const tdBirdeye = document.createElement('td');
      const hasLabel = Boolean(c.correctedEyeState);
      if (c.shadowBirdeyeEye) {
        const labelMap = { eyes_open: 'Eyes Open', eyes_closed: 'Eyes Closed' };
        tdBirdeye.textContent = labelMap[c.shadowBirdeyeEye] || c.shadowBirdeyeEye;
        if (hasLabel) {
          const agreed = c.correctedEyeState === c.shadowBirdeyeEye;
          tdBirdeye.className = 'corr-label ' + (agreed ? 'corr-agree' : 'corr-disagree');
        } else {
          tdBirdeye.className = 'corr-label';
        }
      } else if (c.shadowBirdeyePresent === 0) {
        tdBirdeye.textContent = 'Not Present';
        if (hasLabel) {
          const agreed = c.correctedEyeState === 'not_in_bassinet';
          tdBirdeye.className = 'corr-label ' + (agreed ? 'corr-agree' : 'corr-disagree');
        } else {
          tdBirdeye.className = 'corr-label';
        }
      } else {
        tdBirdeye.textContent = '—';
        tdBirdeye.className = 'corr-label';
      }
      tr.appendChild(tdBirdeye);

      // Source
      const tdSrc = document.createElement('td');
      tdSrc.textContent = c.source || 'dashboard';
      tr.appendChild(tdSrc);

      // Frame thumbnail
      const tdThumb = document.createElement('td');
      if (c.frame) {
        const img = document.createElement('img');
        img.src = frameUrl(c.frame);
        img.className = 'corr-thumb';
        img.onclick = () => showFrameModal(c.frame);
        tdThumb.appendChild(img);
      }
      tr.appendChild(tdThumb);

      tbody.appendChild(tr);
    }
  } catch (e) {
    console.error('Pending corrections error:', e);
  }
}

// ---------------------------------------------------------------------------
// BIRDEYE Classifiers: combined production + training validation view
// ---------------------------------------------------------------------------

// Per-classifier "Last trained" badge — same ET formatting as the global
// meta tag. `classifierKey` is "presence" | "eye_state" | "face_detect"
// (matches the keys emitted by get_last_trained_per_classifier).
function lastTrainedBadge(classifierKey) {
  const per = trainingData && trainingData.lastTrainedPerClassifier;
  const entry = per && per[classifierKey];
  if (!entry || !entry.timestamp) {
    return '<div class="classifier-last-trained classifier-last-trained--empty" '
      + 'title="This classifier has no training_runs row yet">'
      + 'Last trained: never</div>';
  }
  const when = new Date(entry.timestamp).toLocaleString('en-US', {
    timeZone: 'America/New_York', month: 'short', day: 'numeric',
    hour: 'numeric', minute: '2-digit', hour12: true,
  });
  const version = entry.version
    ? ' · <span class="classifier-last-trained-version">' + entry.version + '</span>'
    : '';
  return '<div class="classifier-last-trained" '
    + 'title="Most recent training_runs row whose models_trained included this classifier (accepts presence/eye-state/face-detect singular runs, plus all / all-no-face as appropriate)">'
    + 'Last trained: ' + when + version + '</div>';
}

function delta(curr, prev, suffix, higherIsBetter) {
  if (prev == null || curr == null) return '';
  const diff = curr - prev;
  if (Math.abs(diff) < 0.001) return '';
  const sign = diff > 0 ? '+' : '';
  const good = higherIsBetter ? diff > 0 : diff < 0;
  const color = good ? 'var(--accent-green)' : 'var(--accent-red)';
  return ' <span style="font-size:0.7rem;color:' + color + '">' + sign + (diff * 100).toFixed(1) + suffix + '</span>';
}

// ---- Face-detection sub-panels (3-column grid inside the face column) ----
// These three share a common header style (title + inline description + body).
// The description is plain text in the DOM so it's always visible — the
// previous version relied on title="" hover tooltips which users miss.

function _faceSubHeader(title, scopePill, description) {
  const pill = scopePill
    ? ' <span style="font-weight:400;color:var(--text-muted);font-size:0.7rem">(' + scopePill + ')</span>'
    : '';
  return '<div class="face-sub-header">'
    + '<div class="safety-source-label" style="margin:0">' + title + pill + '</div>'
    + '<div class="face-sub-desc">' + description + '</div>'
    + '</div>';
}

function _renderFaceIouSection(iouBlock) {
  let html = '<div class="face-sub">';
  // Prefer the current-window IoU sample; fall back to all-time when the
  // window is too small. Description states which scope is in use.
  let scope = 'no data';
  let iou = { n: 0 };
  if (iouBlock) {
    const iouWindowed = iouBlock.windowed || { n: 0 };
    const iouAll = iouBlock.allTime || { n: 0 };
    const useWindowed = iouWindowed.n >= 10;
    iou = useWindowed ? iouWindowed : iouAll;
    scope = useWindowed ? 'in window' : 'all time';
  }
  html += _faceSubHeader(
    'IoU vs corrections',
    scope,
    'How tightly the face detector\'s predicted bbox overlaps your dashboard-drawn corrected bbox. IoU (Intersection-over-Union) is the standard bbox-overlap metric: 0 means no overlap, 1 means perfect match. Your drawn bbox is treated as ground truth. 0.5+ is usable; 0.75+ is tight enough for a clean downstream eye-state crop.'
  );

  if (!iouBlock || iou.n === 0) {
    html += '<div class="safety-empty" style="padding:8px 0">No corrected bboxes yet. Use the face-box draw tool on a frame to start populating this.</div>';
    html += '</div>';
    return html;
  }

  html += '<div class="train-details">';
  html += '<div class="train-row"><span>Mean IoU</span><span class="train-val ' + _safetyClass(iou.mean, [0.40, 0.65]) + '">'
    + (iou.mean * 100).toFixed(1) + '%</span></div>';
  html += '<div class="train-row"><span>Median (p50)</span><span class="train-val">'
    + (iou.p50 * 100).toFixed(1) + '%</span></div>';
  html += '<div class="train-row"><span>p10 (worst tail)</span><span class="train-val">'
    + (iou.p10 * 100).toFixed(1) + '%</span></div>';

  const over50Pct = iou.n > 0 ? iou.over50 / iou.n : 0;
  const over75Pct = iou.n > 0 ? iou.over75 / iou.n : 0;
  html += '<div class="train-row"><span>Usable (≥0.5)</span><span class="train-val ' + _safetyClass(over50Pct, [0.70, 0.90]) + '">'
    + iou.over50 + '/' + iou.n + ' (' + Math.round(over50Pct * 100) + '%)</span></div>';
  html += '<div class="train-row"><span>Tight (≥0.75)</span><span class="train-val ' + _safetyClass(over75Pct, [0.40, 0.75]) + '">'
    + iou.over75 + '/' + iou.n + ' (' + Math.round(over75Pct * 100) + '%)</span></div>';
  html += '</div>';

  // If we fell back to allTime, tell the user so they don't read a stale
  // mixed-version average as the current model's behavior.
  if (scope === 'all time') {
    const iouWindowed = iouBlock.windowed || { n: 0 };
    html += '<div class="face-sub-footnote">(only ' + iouWindowed.n + ' corrected bboxes in the current range — showing all-time aggregate instead)</div>';
  }
  html += '</div>';
  return html;
}

function _renderFaceBboxImpactSection(bi, deployedVersion) {
  let html = '<div class="face-sub">';
  html += _faceSubHeader(
    'Bbox impact on eye-state',
    null,
    'Does the face detector\'s bbox actually matter for the downstream eye-state prediction? Same eye-state model, two crops: one from BIRDEYE\'s predicted bbox, one from your corrected bbox. If both predictions agree with your ground-truth label, the bbox wasn\'t a bottleneck. Computed offline by scripts/bbox_impact.py; refreshed after every retrain.'
  );

  if (!bi || !bi.count) {
    html += '<div class="safety-empty" style="padding:8px 0">No data yet — populates when you\'ve drawn corrected bboxes on frames that also have a confirmed eye-state label.</div>';
    html += '</div>';
    return html;
  }

  const predPct = Math.round(bi.accuracyOnPredicted * 100);
  const corrPct = Math.round(bi.accuracyOnCorrected * 100);
  const deltaPts = Math.round(bi.delta * 1000) / 10;
  const deltaSign = deltaPts >= 0 ? '+' : '';
  const deltaColor = deltaPts > 0.5 ? 'var(--accent-green)'
                    : deltaPts < -0.5 ? 'var(--accent-red)'
                    : 'var(--text-muted)';
  const flipPct = Math.round(bi.flipRate * 100);

  html += '<div class="train-details">';
  html += '<div class="train-row"><span>On predicted bbox</span><span class="train-val">'
    + predPct + '% (' + Math.round(bi.accuracyOnPredicted * bi.count) + '/' + bi.count + ')</span></div>';
  html += '<div class="train-row"><span>On corrected bbox</span><span class="train-val">'
    + corrPct + '% (' + Math.round(bi.accuracyOnCorrected * bi.count) + '/' + bi.count + ')</span></div>';
  html += '<div class="train-row"><span>Δ (corrected − predicted)</span><span class="train-val" style="color:' + deltaColor + ';font-weight:600">'
    + deltaSign + deltaPts.toFixed(1) + ' pts</span></div>';
  html += '<div class="train-row"><span>Flip rate</span><span class="train-val">'
    + flipPct + '%</span></div>';
  html += '</div>';
  html += '</div>';
  return html;
}

function _renderFacePerClassSection(bi) {
  let html = '<div class="face-sub">';
  html += _faceSubHeader(
    'Per-class (read this one)',
    null,
    'Same A/B as Bbox impact, split by eye-state class. The aggregate can hide opposite per-class deltas — if the predicted bbox helps eyes_open but hurts eyes_closed, those cancel out in the aggregate. This column tells you which class is benefiting or suffering. Each row reads: accuracy-on-predicted → accuracy-on-corrected, with the delta in points.'
  );

  if (!bi || !bi.perClass || Object.keys(bi.perClass).length === 0) {
    html += '<div class="safety-empty" style="padding:8px 0">No per-class data yet.</div>';
    html += '</div>';
    return html;
  }

  html += '<div class="train-details">';
  for (const cls of Object.keys(bi.perClass)) {
    const pc = bi.perClass[cls];
    const clsDelta = Math.round(pc.delta * 1000) / 10;
    const clsSign = clsDelta >= 0 ? '+' : '';
    const clsColor = clsDelta > 0.5 ? 'var(--accent-green)'
                    : clsDelta < -0.5 ? 'var(--accent-red)'
                    : 'var(--text-muted)';
    html += '<div class="train-row"><span>' + cls + ' (n=' + pc.n + ')</span><span class="train-val" style="color:' + clsColor + '">'
      + Math.round(pc.accuracyOnPredicted * 100) + '% → '
      + Math.round(pc.accuracyOnCorrected * 100) + ' '
      + '<span style="font-weight:600">' + clsSign + clsDelta.toFixed(1) + '</span></span></div>';
  }
  html += '</div>';
  html += '</div>';
  return html;
}

function renderFaceDetectionColumn() {
  const el = document.getElementById('classifier-face');
  if (!el) return;

  const face = safetyData ? safetyData.faceDetection : null;
  if (!face || face.total === 0) {
    el.innerHTML = lastTrainedBadge('face_detect')
      + '<div class="safety-empty">No data yet — populates as new frames arrive with face detection.</div>';
    return;
  }

  let html = lastTrainedBadge('face_detect');

  // --- Production headlines ---
  // Fallback rate already shown in the card's meta line (top of Classifiers
  // card). Confidence distribution dropped — near-binary detector, so
  // min/avg/median/max carries little signal.
  html += '<div class="safety-source-label">Production</div>';
  html += '<div class="safety-headline">';

  html += '<div class="safety-headline-row" title="% of baby-present frames where a face was detected">';
  html += '<span class="safety-headline-label">Detection Rate</span>';
  html += '<span class="safety-headline-value ' + _safetyClass(face.detectionRate, [0.50, 0.75]) + '">'
    + Math.round(face.detectionRate * 100) + '%</span></div>';

  html += '<div class="safety-headline-row">';
  html += '<span class="safety-headline-label">Frames</span>';
  html += '<span class="safety-headline-value">' + face.detected + ' / ' + face.total + '</span></div>';
  html += '</div>';

  // --- IoU vs corrections + Bbox impact + Per-class — laid out in a
  // 3-column sub-grid below so the three related A/B views sit side by
  // side instead of stacking vertically. Each column has its own inline
  // description (browser tooltips on the header are often unnoticed).
  html += '<div class="face-metrics-grid">';
  html += _renderFaceIouSection(face.iou);
  html += _renderFaceBboxImpactSection(face.bboxImpact, safetyData && safetyData.deployedVersion);
  html += _renderFacePerClassSection(face.bboxImpact);
  html += '</div>';

  // Shared staleness / refresh footer — Bbox impact and Per-class both
  // come from bbox_impact.py, so a single footer applies to both.
  if (face.bboxImpact && face.bboxImpact.count > 0) {
    const bi = face.bboxImpact;
    const ranAt = bi.ranAt ? new Date(bi.ranAt).toLocaleString('en-US', {
      timeZone: 'America/New_York', month: 'short', day: 'numeric',
      hour: 'numeric', minute: '2-digit', hour12: true,
    }) : '?';
    const versionMatches = bi.modelVersion === (safetyData && safetyData.deployedVersion);
    const staleWarning = !versionMatches
      ? '<span style="color:var(--accent-red);font-weight:600"> · STALE (run on ' + bi.modelVersion + ')</span>'
      : '';
    html += '<div style="font-size:0.7rem;color:var(--text-muted);margin-top:6px">Bbox impact & Per-class ran ' + ranAt + staleWarning + ' · refresh: <code>python scripts/bbox_impact.py --force</code> (also runs automatically after every dashboard retrain)</div>';
  }

  // --- Training validation metrics ---
  const faceMetrics = trainingData && trainingData.lastMetrics
    ? trainingData.lastMetrics.face_detect : null;
  if (faceMetrics) {
    html += '<details class="cm-details" style="margin-top:14px">';
    html += '<summary class="cm-toggle safety-source-label">Training Validation</summary>';
    html += '<div class="train-details">';

    // Dataset split counts — match the presence/eye-state layout
    if (faceMetrics.train_total != null || faceMetrics.val_total != null) {
      const trainN = faceMetrics.train_total != null ? faceMetrics.train_total : '—';
      const valN = faceMetrics.val_total != null ? faceMetrics.val_total : '—';
      const testN = faceMetrics.test_total != null ? faceMetrics.test_total : '—';
      html += '<div class="train-row"><span title="Number of labeled frames used for train / val / test after per-classifier filtering. For face-detect, a frame qualifies when it has a face bbox label.">Train / Val / Test</span><span class="train-val">'
        + trainN + ' / ' + valN + ' / ' + testN + '</span></div>';
    }

    if (faceMetrics.mean_iou != null) {
      const iouN = faceMetrics.iou_samples != null
        ? ' <span style="color:var(--text-dim);font-weight:400;font-size:0.7rem">(n=' + faceMetrics.iou_samples + ')</span>'
        : '';
      html += '<div class="train-row"><span title="Mean Intersection-over-Union averaged over only the positive validation samples (frames labelled as having a face). iou_samples is that positive count — smaller than val_total.">Mean IoU (val)</span><span class="train-val">'
        + (faceMetrics.mean_iou * 100).toFixed(1) + '%' + iouN + '</span></div>';
    }
    if (faceMetrics.conf_accuracy != null) {
      html += '<div class="train-row"><span title="Binary accuracy: correctly predicting face present vs absent (on val set)">Conf Accuracy (val)</span><span class="train-val">'
        + (faceMetrics.conf_accuracy * 100).toFixed(1) + '%</span></div>';
    }
    // Held-out test metrics — populated by future training runs
    if (faceMetrics.test_mean_iou != null) {
      const testIouN = faceMetrics.test_iou_samples != null
        ? ' <span style="color:var(--text-dim);font-weight:400;font-size:0.7rem">(n=' + faceMetrics.test_iou_samples + ')</span>'
        : '';
      html += '<div class="train-row"><span title="Mean IoU on held-out test set, averaged over positive samples only (test_iou_samples). Not used for model selection, so this is the honest generalization number.">Mean IoU (test)</span><span class="train-val">'
        + (faceMetrics.test_mean_iou * 100).toFixed(1) + '%' + testIouN + '</span></div>';
    }
    if (faceMetrics.test_conf_accuracy != null) {
      html += '<div class="train-row"><span title="Binary present/absent accuracy on held-out test set">Conf Accuracy (test)</span><span class="train-val">'
        + (faceMetrics.test_conf_accuracy * 100).toFixed(1) + '%</span></div>';
    }

    if (faceMetrics.best_epoch != null) {
      html += '<div class="train-row"><span title="Best epoch / total epochs before early stopping">Epochs</span><span class="train-val">'
        + faceMetrics.best_epoch + ' / ' + faceMetrics.total_epochs + '</span></div>';
    }
    if (faceMetrics.val_loss != null) {
      html += '<div class="train-row"><span title="Average per-sample combined loss on val set: BCE(confidence) + 2×SmoothL1(bbox). The 2× weight is applied in the training loop (train_classifiers.py).">Val loss</span><span class="train-val">'
        + faceMetrics.val_loss + '</span></div>';
    }
    html += '</div>';
    html += '</details>';
  }

  el.innerHTML = html;
}

function renderExperiments() {
  // Shadow experiment metrics. The card hides itself when no experiment
  // has any data, so freshly registered experiments aren't visible until
  // the first frame has been processed (live or backfilled).
  const card = document.getElementById('experiments-card');
  const container = document.getElementById('experiments-container');
  if (!card || !container) return;

  const experiments = safetyData && safetyData.experiments;
  if (!experiments) {
    card.style.display = 'none';
    return;
  }
  // Filter to experiments with at least one frame of data
  const names = Object.keys(experiments).filter(n => (experiments[n].count || 0) > 0);
  if (names.length === 0) {
    card.style.display = 'none';
    return;
  }
  card.style.display = '';

  let html = '';
  for (const name of names) {
    const exp = experiments[name];
    const desc = exp.description || '';
    const count = exp.count || 0;
    const gtCount = exp.groundTruthCount || 0;
    const agree = exp.agreementWithProd;
    const agreeDenom = exp.agreementDenom || 0;
    const accExp = exp.accuracyVsGT;
    const accProd = exp.accuracyProdVsGT;
    const delta = exp.deltaVsProd;
    const lat = exp.avgLatencyMs;
    const ver = exp.modelVersion || '?';
    const perClass = exp.perClass || {};

    // Stale flag: cached model version doesn't match the latest deployed
    // version of the experiment's underlying checkpoint. We can't read that
    // directly from the API right now, so we just show the version inline
    // and let the user spot drift.
    html += '<div class="experiment-block" style="border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:12px">';
    html += '<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px">';
    html += '<span style="font-weight:600;font-size:0.95rem">' + name + '</span>';
    html += '<span style="font-size:0.7rem;color:var(--text-muted)">model ' + ver + ' · n=' + count + '</span>';
    html += '</div>';
    html += '<div style="font-size:0.75rem;color:var(--text-muted);margin-bottom:8px">' + desc + '</div>';

    // Top-line numbers — three columns
    html += '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:10px">';

    // Agreement with prod
    html += '<div>';
    html += '<div style="font-size:0.7rem;color:var(--text-muted)" title="Fraction of frames where this experiment\'s eyeState matches what BIRDEYE prod predicted at capture time. Tells you how often the experiment changes the answer.">Agreement w/ prod</div>';
    if (agree != null) {
      html += '<div style="font-size:1.1rem;font-weight:600">' + Math.round(agree * 100) + '%</div>';
      html += '<div style="font-size:0.65rem;color:var(--text-muted)">' + Math.round(agree * agreeDenom) + ' / ' + agreeDenom + ' frames</div>';
    } else {
      html += '<div style="font-size:1.1rem;color:var(--text-muted)">—</div>';
    }
    html += '</div>';

    // Accuracy vs GT (experiment)
    html += '<div>';
    html += '<div style="font-size:0.7rem;color:var(--text-muted)" title="Experiment\'s accuracy on frames you have reviewed or corrected — the closest thing to ground truth available.">Accuracy vs GT (exp)</div>';
    if (accExp != null) {
      html += '<div style="font-size:1.1rem;font-weight:600">' + Math.round(accExp * 100) + '%</div>';
      html += '<div style="font-size:0.65rem;color:var(--text-muted)">' + Math.round(accExp * gtCount) + ' / ' + gtCount + ' frames</div>';
    } else {
      html += '<div style="font-size:1.1rem;color:var(--text-muted)">—</div>';
    }
    html += '</div>';

    // Delta vs prod
    html += '<div>';
    html += '<div style="font-size:0.7rem;color:var(--text-muted)" title="Experiment accuracy minus prod accuracy on the same ground-truth set. Positive = experiment is better.">Δ vs prod (GT)</div>';
    if (delta != null) {
      const deltaPts = Math.round(delta * 1000) / 10;
      const deltaSign = deltaPts >= 0 ? '+' : '';
      const deltaColor = deltaPts > 0.5 ? 'var(--accent-green)'
                        : deltaPts < -0.5 ? 'var(--accent-red)'
                        : 'var(--text-muted)';
      html += '<div style="font-size:1.1rem;font-weight:600;color:' + deltaColor + '">' + deltaSign + deltaPts.toFixed(1) + ' pts</div>';
      if (accProd != null) {
        html += '<div style="font-size:0.65rem;color:var(--text-muted)">prod was ' + Math.round(accProd * 100) + '%</div>';
      }
    } else {
      html += '<div style="font-size:1.1rem;color:var(--text-muted)">—</div>';
    }
    html += '</div>';

    html += '</div>';

    // Per-class breakdown
    if (perClass && Object.keys(perClass).length > 0) {
      html += '<div style="font-size:0.7rem;color:var(--text-muted);margin-bottom:4px">Per-class (where the real story usually lives)</div>';
      html += '<div class="train-details">';
      for (const cls of Object.keys(perClass)) {
        const pc = perClass[cls];
        const clsDelta = Math.round(pc.delta * 1000) / 10;
        const clsSign = clsDelta >= 0 ? '+' : '';
        const clsColor = clsDelta > 0.5 ? 'var(--accent-green)'
                        : clsDelta < -0.5 ? 'var(--accent-red)'
                        : 'var(--text-muted)';
        html += '<div class="train-row"><span>' + cls + ' (n=' + pc.n + ')</span><span class="train-val" style="color:' + clsColor + '">'
          + Math.round(pc.accuracyProd * 100) + '% → '
          + Math.round(pc.accuracyExp * 100) + '% '
          + '<span style="font-weight:600">' + clsSign + clsDelta.toFixed(1) + '</span></span></div>';
      }
      html += '</div>';
    }

    // Latency footer
    if (lat != null) {
      html += '<div style="font-size:0.65rem;color:var(--text-muted);margin-top:6px">avg latency: ' + lat + ' ms / frame</div>';
    }
    html += '</div>';
  }

  container.innerHTML = html;
}

function renderClassifiers() {
  renderDataColumn();
  renderClassifierColumn('classifier-presence', 'presence');
  renderFaceDetectionColumn();
  renderClassifierColumn('classifier-eye', 'eye_state');
  renderExperiments();

  // Meta line: model version, deployed version, training status, rollback
  const metaEl = document.getElementById('classifiers-meta');
  if (metaEl) {
    let tags = [];

    // Deployed version (from safety data)
    if (safetyData && safetyData.deployedVersion) {
      tags.push('<span class="meta-tag">deployed: ' + safetyData.deployedVersion + '</span>');
    }

    // Rollback warning
    if (safetyData && safetyData.rolledBack) {
      tags.push('<span class="meta-tag meta-warn">rolled back from ' + safetyData.latestTrainedVersion + '</span>');
    }

    // Training info
    if (trainingData && trainingData.lastMetrics) {
      const trainedAt = trainingData.lastTrained
        ? new Date(trainingData.lastTrained).toLocaleString('en-US', {
            timeZone: 'America/New_York', month: 'short', day: 'numeric',
            hour: 'numeric', minute: '2-digit', hour12: true })
        : '?';
      tags.push('<span class="meta-tag">trained: ' + trainedAt + '</span>');

      // Pending corrections
      const pending = trainingData.pendingCorrections || 0;
      if (pending > 0) {
        tags.push('<span class="meta-tag meta-warn">' + pending + ' corrections pending</span>');
      }
    }

    // Fallback rate from face detection
    if (safetyData && safetyData.faceDetection) {
      const fd = safetyData.faceDetection;
      const fbPct = Math.round(fd.fallbackRate * 100);
      const fbColor = fbPct > 20 ? 'meta-warn' : '';
      tags.push('<span class="meta-tag ' + fbColor + '" title="% of baby-present frames where face detection failed → cloud API fallback">'
        + 'fallback: ' + fbPct + '% (' + (fd.total - fd.detected) + '/' + fd.total + ')</span>');
    }

    // Run status
    if (trainingData) {
      if (trainingData.running) {
        let elapsed = '';
        if (trainingData.startedAt) {
          const sec = Math.round((Date.now() - new Date(trainingData.startedAt).getTime()) / 1000);
          elapsed = sec >= 60 ? ' (' + Math.floor(sec / 60) + 'm ' + (sec % 60) + 's)' : ' (' + sec + 's)';
        }
        tags.push('<span class="meta-tag" style="color:var(--accent-blue)">training' + elapsed + '</span>');
      } else if (trainingData.runStatus === 'failed') {
        tags.push('<span class="meta-tag meta-warn">last run failed</span>');
      } else if (trainingData.runStatus === 'aborted') {
        tags.push('<span class="meta-tag meta-warn">last run aborted</span>');
      }
    }

    metaEl.innerHTML = tags.join('');
  }
}

function renderDataColumn() {
  const dataEl = document.getElementById('train-data');
  if (!trainingData || !trainingData.lastMetrics) {
    dataEl.innerHTML = '<div class="safety-empty">No training data yet.</div>';
    return;
  }

  const sources = trainingData.lastLabelSources || {};
  const total = trainingData.lastEntriesTotal || 0;
  const pending = trainingData.pendingCorrections || 0;
  const totalCorr = trainingData.totalCorrections || 0;
  const trained = totalCorr - pending;
  const pendingColor = pending > 0 ? 'var(--accent-orange)' : 'var(--text-dim)';

  let html = '';
  html += '<div class="train-row"><span title="Corrections made since last training — included in next retrain">Pending corrections</span><span class="train-val" style="color:' + pendingColor + '">' + pending + '</span></div>';
  html += '<div class="train-row"><span title="Corrections already used in previous training runs">Trained corrections</span><span class="train-val">' + trained + '</span></div>';

  if (trainingData.prevVersion) {
    html += '<div class="train-row"><span title="Previous model for delta comparison">Previous model</span><span class="train-val">' + trainingData.prevVersion + '</span></div>';
  }

  // Run timing
  const durStats = trainingData.trainingDurationStats || {};
  const lastDurSec = trainingData.lastDurationSeconds;
  if (lastDurSec != null || durStats.avg_seconds != null) {
    html += '<div style="margin:8px 0 2px;font-size:0.7rem;color:#445">Run timing:</div>';
    if (lastDurSec != null)
      html += '<div class="train-row"><span>Last run</span><span class="train-val">' + formatSeconds(lastDurSec) + '</span></div>';
    if (durStats.avg_seconds != null)
      html += '<div class="train-row"><span>Avg (' + (durStats.count || 0) + ' runs)</span><span class="train-val">' + formatSeconds(durStats.avg_seconds) + '</span></div>';
    if (durStats.p99_seconds != null)
      html += '<div class="train-row"><span>p99</span><span class="train-val">' + formatSeconds(durStats.p99_seconds) + '</span></div>';
  }

  // Training data sources
  const srcRows = Object.entries(sources).map(([k,v]) =>
    '<div class="train-row"><span>' + k + '</span><span class="train-val">' + v + '</span></div>'
  ).join('');
  if (total > 0) {
    html += '<div style="margin:8px 0 2px;font-size:0.7rem;color:#445">Training data:</div>';
    html += '<div class="train-row"><span>Total entries</span><span class="train-val">' + total + '</span></div>';
    html += srcRows;
  }

  dataEl.innerHTML = html;
}

function renderClassifierColumn(elId, type) {
  const el = document.getElementById(elId);
  if (!el) return;

  const isPresence = type === 'presence';
  const classes = isPresence ? ['not_present', 'present'] : ['eyes_open', 'eyes_closed'];
  let html = lastTrainedBadge(isPresence ? 'presence' : 'eye_state');

  // --- vs Corrections (ground truth) ---
  const safety = safetyData ? (isPresence ? safetyData.presence : safetyData.eyeState) : null;

  if (safety) {
    const bird = safety.birdeyeVsGT || {};
    const gt = safetyData.groundTruth || {};
    const macroThresh = isPresence ? [0.90, 0.97] : [0.60, 0.85];
    const accThresh = isPresence ? [0.90, 0.97] : [0.75, 0.90];
    const hasBird = bird.total > 0;

    // Ground truth source label
    const gtLabel = gt.total > 0
      ? '(' + gt.total + ' labels: ' + gt.reviewed + ' reviewed, ' + gt.corrected + ' corrected)'
      : '';
    html += '<div class="safety-source-label">vs Ground Truth <span style="color:#556;font-weight:400">' + gtLabel + '</span></div>';
    html += '<div class="safety-headline">';

    if (hasBird) {
      html += '<div class="safety-headline-row" title="BIRDEYE macro F1 against reviewed + corrected ground truth"><span class="safety-headline-label">BIRDEYE Macro F1</span>';
      html += '<span class="safety-headline-value ' + _safetyClass(bird.macroF1, macroThresh) + '">'
        + Math.round(bird.macroF1 * 100) + '% <span style="font-size:0.7rem;color:var(--text-dim)">(' + bird.total + ')</span></span></div>';

      html += '<div class="safety-headline-row" title="BIRDEYE accuracy against ground truth"><span class="safety-headline-label">BIRDEYE Accuracy</span>';
      html += '<span class="safety-headline-value ' + _safetyClass(bird.accuracy, accThresh) + '">'
        + Math.round(bird.accuracy * 100) + '%</span></div>';
    }
    html += '</div>';

    // BIRDEYE per-class P/R/F1 + collapsible confusion matrix
    if (hasBird && bird.confusion) {
      html += '<div class="safety-source-label">BIRDEYE vs Ground Truth</div>';
      html += _renderPerClass(bird, classes);
      html += '<details class="cm-details"><summary class="cm-toggle">Confusion matrix</summary>';
      html += _renderConfusion(bird, classes);
      html += '</details>';
    }

    if (!hasBird) {
      html += '<div class="safety-empty">No ground truth data yet — review blocks in the timeline to build ground truth.</div>';
    }
  } else {
    html += '<div class="safety-empty">Loading metrics...</div>';
  }

  // --- Training validation metrics ---
  const metrics = trainingData && trainingData.lastMetrics
    ? (isPresence ? trainingData.lastMetrics.presence : trainingData.lastMetrics.eye_state)
    : null;
  const prevMetrics = trainingData && trainingData.prevMetrics
    ? (isPresence ? trainingData.prevMetrics.presence : trainingData.prevMetrics.eye_state)
    : null;

  if (metrics) {
    const pm = prevMetrics || {};
    html += '<details class="cm-details" style="margin-top:14px">';
    html += '<summary class="cm-toggle safety-source-label">Training Validation</summary>';
    html += '<div class="train-details">';

    // --- Dataset split counts ---
    // train_total / val_total come from the Dataset class's length after
    // per-classifier filtering. test_total is written by future training
    // runs that evaluate on the held-out test split; older runs show "—"
    // until a retrain lands or the backfill script patches them.
    const trainN = metrics.train_total != null ? metrics.train_total : '—';
    const valN = metrics.val_total != null ? metrics.val_total : '—';
    const testN = metrics.test_total != null ? metrics.test_total : '—';
    html += '<div class="train-row"><span title="Number of labeled frames used for train / val / test after per-classifier filtering. Splits are time-block deterministic (SEED=42, 30-min blocks). Test is held out — val is used for best-epoch selection.">Train / Val / Test</span><span class="train-val">'
      + trainN + ' / ' + valN + ' / ' + testN + '</span></div>';

    html += '<div class="train-row"><span title="% of validation samples correctly classified during training (val set is used for best-epoch selection, so this is optimistically biased)">Val accuracy</span><span class="train-val">'
      + (metrics.val_accuracy * 100).toFixed(1) + '%'
      + delta(metrics.val_accuracy, pm.val_accuracy, '%', true)
      + '</span></div>';

    if (metrics.best_macro_f1 != null) {
      html += '<div class="train-row"><span title="Macro-averaged F1 on validation set. Best-model selection criterion.">Macro F1 (val)</span><span class="train-val">'
        + (metrics.best_macro_f1 * 100).toFixed(1) + '%'
        + delta(metrics.best_macro_f1, pm.best_macro_f1, '%', true)
        + '</span></div>';
    }

    // --- Held-out test metrics ---
    // Populated by training runs that evaluate on test_entries at the
    // end of training. This is the honest generalization signal — the
    // val metrics above are optimistically biased because val is used
    // for best-epoch selection.
    if (metrics.test_accuracy != null) {
      html += '<div class="train-row"><span title="Test-set accuracy on the held-out split — not used for model selection, so this is the honest generalization number. Diverges from val accuracy when the model has overfit.">Test accuracy</span><span class="train-val">'
        + (metrics.test_accuracy * 100).toFixed(1) + '%'
        + delta(metrics.test_accuracy, pm.test_accuracy, '%', true)
        + '</span></div>';
    }
    if (metrics.test_macro_f1 != null) {
      html += '<div class="train-row"><span title="Macro-F1 on held-out test set — compare against val macro-F1 to see how much best-epoch selection is overfitting to val.">Macro F1 (test)</span><span class="train-val">'
        + (metrics.test_macro_f1 * 100).toFixed(1) + '%'
        + delta(metrics.test_macro_f1, pm.test_macro_f1, '%', true)
        + '</span></div>';
    }

    html += '<div class="train-row"><span title="Epoch with best macro-F1 / total epochs before early stopping">Epochs</span><span class="train-val">'
      + metrics.best_epoch + ' / ' + metrics.total_epochs + '</span></div>';

    html += '<div class="train-row"><span title="Cross-entropy loss on validation set (tracked, not used for selection)">Val loss</span><span class="train-val">'
      + metrics.best_val_loss
      + delta(metrics.best_val_loss, pm.best_val_loss, '', false)
      + '</span></div>';

    if (metrics.per_class) {
      html += '<div style="margin-top:4px;font-size:0.72rem;color:var(--text-dim)">Per-class P / R / F1:</div>';
      for (const [cls, s] of Object.entries(metrics.per_class)) {
        const ps = pm.per_class && pm.per_class[cls];
        html += '<div class="train-row"><span>' + cls + ' <span style="color:#556">(' + s.support + ')</span></span><span class="train-val">'
          + s.precision + ' / ' + s.recall + ' / ' + s.f1
          + (ps ? delta(s.f1, ps.f1, '', true) : '')
          + '</span></div>';
      }
    }

    html += '</div>';
    html += '</details>';
  }

  el.innerHTML = html;
}

// ---------------------------------------------------------------------------
// Recent events table
// ---------------------------------------------------------------------------
async function loadEvents() {
  try {
    const count = document.getElementById('events-count').value;
    const type = document.getElementById('events-type').value;
    const hours = document.getElementById('events-range').value;
    const res = await fetch(
      '/api/events?count=' + count
      + '&type=' + encodeURIComponent(type)
      + '&hours=' + encodeURIComponent(hours)
    );
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

    renderClassifiers();
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
      body: JSON.stringify({
        trigger: 'dashboard',
        skipFaceDetect: document.getElementById('skip-face-detect').checked,
      }),
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

    // Prod cost + estimated monthly
    if (d.cost) {
      const hoursNum = parseFloat(hours);
      const monthlyEst = hoursNum > 0 ? (d.cost.estCost / hoursNum * 730).toFixed(0) : '?';
      document.getElementById('perf-cloud-cost').innerHTML =
        '$' + d.cost.estCost.toFixed(2) + ' (' + d.cost.apiCalls + ' calls)' +
        '<div style="font-size:0.65rem;color:var(--text-dim);margin-top:2px">~$' + monthlyEst + '/mo est</div>';
    } else {
      document.getElementById('perf-cloud-cost').textContent = '--';
    }

    // Prod latency (avg + p99) — BIRDEYE is prod since the cascade flip
    if (d.timing) {
      document.getElementById('perf-latency').innerHTML =
        Math.round(d.timing.avg * 1000) + 'ms avg' +
        (d.timing.p99 != null ? '<div style="font-size:0.65rem;color:var(--text-dim);margin-top:2px">p99: ' + Math.round(d.timing.p99 * 1000) + 'ms</div>' : '');
    } else {
      document.getElementById('perf-latency').textContent = '--';
    }

    // Gaps
    document.getElementById('perf-gaps').textContent = d.gaps != null ? d.gaps : '--';

    // Breakdown bars inside #perf-breakdown:
    //   1. Production pipeline (pixel-diff / birdeye / cloud API decision source)
    //   2. BIRDEYE model versions (which versioned checkpoint produced the
    //      birdeye-decided frames — only rendered when there are birdeye frames)
    const breakdown = document.getElementById('perf-breakdown');
    if (d.total > 0) {
      const bCount = d.methods.birdeye || 0;
      const cCount = d.methods.cloud_api || 0;
      const pCount = d.methods.pixel_diff || 0;
      const bPct = Math.round(bCount / d.total * 100);
      const cPct = Math.round(cCount / d.total * 100);
      const pPct = Math.round(pCount / d.total * 100);

      let html =
        '<div style="margin-bottom:6px;font-size:0.75rem;color:var(--text-dim)">Production decision source</div>' +
        '<div class="perf-bar">' +
          (pPct > 0 ? '<div class="perf-bar-seg pixel-diff" style="width:' + pPct + '%" title="Pixel-diff ' + pCount + ' (' + pPct + '%)">' + (pPct > 5 ? pPct + '%' : '') + '</div>' : '') +
          (bPct > 0 ? '<div class="perf-bar-seg birdeye" style="width:' + bPct + '%" title="BIRDEYE ' + bCount + ' (' + bPct + '%)">' + (bPct > 5 ? bPct + '%' : '') + '</div>' : '') +
          (cPct > 0 ? '<div class="perf-bar-seg cloud" style="width:' + cPct + '%" title="Cloud API ' + cCount + ' (' + cPct + '%)">' + (cPct > 5 ? cPct + '%' : '') + '</div>' : '') +
        '</div>' +
        '<div class="perf-bar-legend">' +
          '<span><span class="legend-dot" style="background:var(--accent-blue)"></span> Pixel-diff (' + pCount + ')</span>' +
          '<span><span class="legend-dot" style="background:var(--accent-green)"></span> BIRDEYE (' + bCount + ')</span>' +
          '<span><span class="legend-dot" style="background:var(--accent-orange)"></span> Cloud API (' + cCount + ')</span>' +
        '</div>';

      // BIRDEYE model versions breakdown — which versioned checkpoint
      // produced the birdeye-decided frames. Only render if we have any.
      const versions = d.birdeyeVersions || {};
      const versionEntries = Object.entries(versions).sort((a, b) => b[1] - a[1]);
      if (versionEntries.length > 0 && bCount > 0) {
        // Top 4 versions + "older" rollup so the bar stays readable when
        // retraining has shipped many versions in the window.
        const topN = 4;
        const top = versionEntries.slice(0, topN);
        const rest = versionEntries.slice(topN);
        const restCount = rest.reduce((s, [, n]) => s + n, 0);
        const shortLabel = (v) => {
          // v_20260412_141928 → 04/12 14:19  (month/day + time)
          const m = /^v_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})/.exec(v);
          return m ? (m[2] + '/' + m[3] + ' ' + m[4] + ':' + m[5]) : v;
        };
        const palette = [
          'var(--accent-green)',
          'var(--accent-blue)',
          'var(--accent-blue-light)',
          'var(--accent-orange)',
          'var(--text-dim)',
        ];

        html += '<div style="margin:14px 0 6px;font-size:0.75rem;color:var(--text-dim)">BIRDEYE model versions</div>';
        html += '<div class="perf-bar">';
        top.forEach(([ver, n], i) => {
          const pct = Math.round(n / bCount * 100);
          if (pct > 0) {
            html += '<div class="perf-bar-seg" style="width:' + pct + '%;background:' + palette[i] + '" title="' + ver + ' — ' + n + ' frames (' + pct + '% of BIRDEYE)">' + (pct > 8 ? pct + '%' : '') + '</div>';
          }
        });
        if (restCount > 0) {
          const pct = Math.round(restCount / bCount * 100);
          html += '<div class="perf-bar-seg" style="width:' + pct + '%;background:' + palette[4] + '" title="' + rest.length + ' older versions — ' + restCount + ' frames">' + (pct > 8 ? pct + '%' : '') + '</div>';
        }
        html += '</div>';
        html += '<div class="perf-bar-legend">';
        top.forEach(([ver, n], i) => {
          html += '<span><span class="legend-dot" style="background:' + palette[i] + '"></span> ' + shortLabel(ver) + ' (' + n + ')</span>';
        });
        if (restCount > 0) {
          html += '<span><span class="legend-dot" style="background:' + palette[4] + '"></span> older ×' + rest.length + ' (' + restCount + ')</span>';
        }
        html += '</div>';
      }

      breakdown.innerHTML = html;
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
  visible: 'visible',
  not_visible: 'not visible',
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

async function loadSafetyStats() {
  try {
    const hours = document.getElementById('safety-range').value;
    const res = await fetch('/api/safety-stats?hours=' + hours);
    safetyData = await res.json();

    const rangeLabel = {'6':'6h','12':'12h','24':'24h','168':'7d'}[hours] || hours+'h';
    const total = safetyData.shadowTotal || 0;
    const periodEl = document.getElementById('safety-period');
    periodEl.textContent = '(' + rangeLabel + ', ' + total + ' frames with baby in bassinet)';
    periodEl.title = 'BIRDEYE shadow inference only runs on frames where the baby is present in the bassinet — empty-bassinet frames are excluded from this count and from all classifier metrics in this card.';

    renderClassifiers();
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
    loadBassinetChart(),
    loadPendingCorrections(),
    loadPipelineHistory(),
    loadEyeStateDailyMetrics(),
    loadSystemUsage(),
  ]);
  document.getElementById('footer-refresh').textContent =
    'Last refreshed: ' + new Date().toLocaleTimeString('en-US', { timeZone: 'America/New_York' });
}

// ---------------------------------------------------------------------------
// System Load (Models tab)
// ---------------------------------------------------------------------------
// Backed by /api/system-usage (dashboard/system_usage.py). Polled every 10s
// so the panel tracks in close-to-real-time — useful when the user kicks
// off a retrain from the dashboard and wants to watch the load climb.

function _fmtBytes(n) {
  if (n == null) return '—';
  const units = [['TB', 1e12], ['GB', 1e9], ['MB', 1e6], ['KB', 1e3]];
  for (const [u, d] of units) {
    if (n >= d) return (n / d).toFixed(1) + ' ' + u;
  }
  return n + ' B';
}

function _loadRatioClass(ratio) {
  // Thresholds are tuned for a laptop, not a server. 1.0× is fully used,
  // >1.0 means the run queue is backed up. Sustained >2× is where macOS
  // starts noticeably lagging.
  if (ratio >= 2.0) return 'bad';
  if (ratio >= 1.2) return 'warn';
  return 'good';
}

function _trendArrow(trend) {
  if (trend > 0.3) return '↑';
  if (trend < -0.3) return '↓';
  return '→';
}

async function loadSystemUsage() {
  try {
    const res = await fetch('/api/system-usage');
    if (!res.ok) throw new Error('http ' + res.status);
    const data = await res.json();
    _renderSystemUsage(data);
  } catch (err) {
    console.error('system-usage error:', err);
    const el = document.getElementById('system-usage-body');
    if (el) el.innerHTML = '<div class="safety-empty">Failed to load system usage.</div>';
  }
}

function _renderSystemUsage(data) {
  const body = document.getElementById('system-usage-body');
  const asOfEl = document.getElementById('system-usage-asof');
  const refreshEl = document.getElementById('system-usage-refresh');
  if (!body) return;

  if (asOfEl) {
    asOfEl.textContent = data.asOf
      ? '(as of ' + new Date(data.asOf).toLocaleTimeString('en-US', {
          timeZone: 'America/New_York', hour: 'numeric', minute: '2-digit',
          second: '2-digit', hour12: true,
        }) + ')'
      : '';
  }
  if (refreshEl) refreshEl.textContent = 'auto-refreshes every 10s';

  const load = data.load || {};
  const mem = data.memory || {};
  const disk = data.disk || [];
  const bm = (data.babyMonitor && data.babyMonitor.sizes) || {};
  const bmProcs = (data.babyMonitor && data.babyMonitor.processes) || [];
  const top = data.topProcesses || [];

  let html = '';

  // --- Headline row: load + memory + disk at a glance ---
  html += '<div class="sys-headline-row">';

  // Load
  const ratioCls = _loadRatioClass(load.ratio || 0);
  const arrow = _trendArrow(load.trend || 0);
  html += '<div class="sys-tile">';
  html += '<div class="sys-tile-label">Load (1 / 5 / 15 min)</div>';
  html += '<div class="sys-tile-value ' + ratioCls + '">'
    + (load.oneMin != null ? load.oneMin.toFixed(2) : '—')
    + ' <span class="sys-tile-sub">/ ' + (load.fiveMin != null ? load.fiveMin.toFixed(2) : '—')
    + ' / ' + (load.fifteenMin != null ? load.fifteenMin.toFixed(2) : '—')
    + '</span></div>';
  html += '<div class="sys-tile-note" title="Unix load average divided by core count. >1.0 means the run queue is longer than the CPU can process in real time.">'
    + (load.cores || '?') + ' cores · ratio '
    + (load.ratio != null ? load.ratio.toFixed(2) + '× ' + arrow : '—')
    + '</div>';
  html += '</div>';

  // Memory
  const memCls = mem.usedPct == null ? ''
    : (mem.usedPct >= 90 ? 'bad' : (mem.usedPct >= 75 ? 'warn' : 'good'));
  html += '<div class="sys-tile">';
  html += '<div class="sys-tile-label">Memory</div>';
  html += '<div class="sys-tile-value ' + memCls + '">'
    + (mem.usedPct != null ? mem.usedPct.toFixed(1) + '%' : '—')
    + '</div>';
  html += '<div class="sys-tile-note" title="Pages that are free, active, inactive, wired, or compressed, reported via vm_stat. usedPct = (total − free) / total.">'
    + _fmtBytes(mem.totalBytes) + ' total · '
    + _fmtBytes(mem.compressedBytes) + ' compressed'
    + '</div>';
  html += '</div>';

  // Disk (workspace row only; root is redundant on single-APFS-volume macs)
  const workspace = disk.find((d) => d.label === 'workspace') || disk[0];
  if (workspace) {
    const diskCls = workspace.usedPct >= 95 ? 'bad'
      : (workspace.usedPct >= 85 ? 'warn' : 'good');
    html += '<div class="sys-tile">';
    html += '<div class="sys-tile-label">Disk (workspace volume)</div>';
    html += '<div class="sys-tile-value ' + diskCls + '">'
      + workspace.usedPct.toFixed(1) + '%</div>';
    html += '<div class="sys-tile-note">'
      + _fmtBytes(workspace.freeBytes) + ' free · '
      + _fmtBytes(workspace.totalBytes) + ' total'
      + '</div>';
    html += '</div>';
  }

  // Baby-monitor data sizes
  html += '<div class="sys-tile">';
  html += '<div class="sys-tile-label">Baby-monitor data</div>';
  html += '<div class="sys-tile-value">' + _fmtBytes(bm.dataDirBytes) + '</div>';
  html += '<div class="sys-tile-note">'
    + 'frames ' + _fmtBytes(bm.framesDirBytes) + ' · '
    + 'models ' + _fmtBytes(bm.modelsDirBytes) + ' · '
    + 'db ' + _fmtBytes(bm.monitorDbBytes)
    + '</div>';
  html += '</div>';

  html += '</div>';

  // --- Baby-monitor processes (always shown; explains whose load this is) ---
  html += '<div class="sys-section-label">Baby-monitor processes</div>';
  if (bmProcs.length === 0) {
    html += '<div class="safety-empty" style="padding:4px 0">'
      + 'None of the known baby-monitor scripts are running right now. '
      + '(Expected: dashboard/app.py is always up; monitor.py is ephemeral per 1-min tick.)'
      + '</div>';
  } else {
    html += _sysProcessTable(bmProcs, { showScript: true });
  }

  // --- Top CPU processes ---
  html += '<div class="sys-section-label">Top processes (by CPU)</div>';
  if (top.length === 0) {
    html += '<div class="safety-empty" style="padding:4px 0">No process data.</div>';
  } else {
    html += _sysProcessTable(top.slice(0, 6), { showScript: false });
  }

  body.innerHTML = html;
}

function _sysProcessTable(rows, opts) {
  let html = '<table class="sys-proc-table"><thead><tr>';
  html += '<th>PID</th>';
  if (opts.showScript) html += '<th>Script</th>';
  html += '<th>CPU</th><th>Mem</th><th>RSS</th><th>Elapsed</th><th>Command</th>';
  html += '</tr></thead><tbody>';
  for (const p of rows) {
    const hotCls = p.cpuPct >= 100 ? 'sys-proc-hot'
      : (p.cpuPct >= 25 ? 'sys-proc-warm' : '');
    html += '<tr class="' + hotCls + '">';
    html += '<td class="num">' + p.pid + '</td>';
    if (opts.showScript) {
      html += '<td><code>' + (p.script || '—') + '</code></td>';
    }
    html += '<td class="num">' + (p.cpuPct != null ? p.cpuPct.toFixed(1) + '%' : '—') + '</td>';
    html += '<td class="num">' + (p.memPct != null ? p.memPct.toFixed(1) + '%' : '—') + '</td>';
    html += '<td class="num">' + _fmtBytes((p.rssKb || 0) * 1024) + '</td>';
    html += '<td class="num">' + (p.etime || '—') + '</td>';
    html += '<td>' + (p.command || '—') + '</td>';
    html += '</tr>';
  }
  html += '</tbody></table>';
  return html;
}

// Kick off a 10s polling loop for the System Load card. Separate from the
// main loadAll() cadence so it's lightweight and refreshes during retrain
// without jittering the other panels.
setInterval(loadSystemUsage, 10000);

// --- Tab switching ---
// The main content is split into three tabs: Monitor (live watching),
// Models (pending corrections, BIRDEYE classifiers, shadow experiments,
// pipeline stats), and Events (recent events table). Active tab is
// persisted in localStorage and reflected in the URL hash so links and
// reloads land on the right view.
function setActiveTab(name, opts) {
  const push = opts && opts.push;
  const buttons = document.querySelectorAll('.tab-btn');
  const panels = document.querySelectorAll('.tab-panel');
  let matched = false;
  buttons.forEach((b) => {
    const isActive = b.dataset.tab === name;
    b.classList.toggle('active', isActive);
    b.setAttribute('aria-selected', isActive ? 'true' : 'false');
    if (isActive) matched = true;
  });
  panels.forEach((p) => {
    const isActive = p.dataset.tab === name;
    p.classList.toggle('active', isActive);
    // [hidden] is used so inactive panels are also removed from the
    // accessibility tree. The CSS rule for .tab-panel.active overrides it.
    if (isActive) {
      p.removeAttribute('hidden');
    } else {
      p.setAttribute('hidden', '');
    }
  });
  if (!matched) return;
  try {
    localStorage.setItem('bilbo:tab', name);
  } catch (e) { /* storage blocked or full — ignore */ }
  if (push && window.location.hash !== '#' + name) {
    history.replaceState(null, '', '#' + name);
  }
}

function initTabs() {
  const validTabs = ['monitor', 'models', 'events'];
  // 'recap' was a separate tab until 2026-04-18 when it was merged into
  // Events; redirect old hash/localStorage values so bookmarks still land.
  const redirect = (name) => (name === 'recap' ? 'events' : name);
  let initial = 'monitor';
  const fromHash = redirect((window.location.hash || '').replace('#', ''));
  if (validTabs.includes(fromHash)) {
    initial = fromHash;
  } else {
    try {
      const stored = redirect(localStorage.getItem('bilbo:tab'));
      if (validTabs.includes(stored)) initial = stored;
    } catch (e) { /* ignore */ }
  }
  setActiveTab(initial, { push: true });

  document.querySelectorAll('.tab-btn').forEach((btn) => {
    btn.addEventListener('click', () => setActiveTab(btn.dataset.tab, { push: true }));
  });

  window.addEventListener('hashchange', () => {
    const name = (window.location.hash || '').replace('#', '');
    if (validTabs.includes(name)) setActiveTab(name, { push: false });
  });
}

// --- Pipeline History table (Models tab) ---
// Per-ET-day breakdown of how each capture was decided (pixel-diff,
// BIRDEYE, or cloud API fallback) plus the cloud-API cost and the
// dominant BIRDEYE model version(s) for that day. Powered by
// /api/pipeline-history.
async function loadPipelineHistory() {
  const days = document.getElementById('pipeline-history-days').value;
  const body = document.getElementById('pipeline-history-body');
  const note = document.getElementById('pipeline-history-note');
  try {
    const res = await fetch('/api/pipeline-history?days=' + encodeURIComponent(days));
    const data = await res.json();
    const rows = data.rows || [];
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="7" class="muted">No data in this range.</td></tr>';
      note.textContent = '';
      return;
    }

    const fmtCell = (c) => c && c.count ? `${c.count} <span class="muted">(${c.pct.toFixed(1)}%)</span>` : '0';
    const fmtVersions = (versions) => {
      if (!versions || !versions.length) return '<span class="muted">—</span>';
      // Top 3 to keep the cell compact; one per line.
      return versions.slice(0, 3).map(v =>
        `<div class="version-line"><code>${v.version}</code> <span class="muted">(${v.pct.toFixed(1)}%)</span></div>`
      ).join('');
    };

    // Newest first — easier for at-a-glance.
    const sorted = [...rows].sort((a, b) => b.date.localeCompare(a.date));
    body.innerHTML = sorted.map(r => `
      <tr>
        <td>${r.date}</td>
        <td class="num">${r.captures}</td>
        <td class="num">${fmtCell(r.pixelDiff)}</td>
        <td class="num">${fmtCell(r.birdeye)}</td>
        <td class="num">${fmtCell(r.cloudApi)}</td>
        <td class="num">$${r.cost.toFixed(2)}</td>
        <td>${fmtVersions(r.versions)}</td>
      </tr>
    `).join('');

    const totalCost = rows.reduce((s, r) => s + (r.cost || 0), 0);
    const totalCaptures = rows.reduce((s, r) => s + (r.captures || 0), 0);
    note.textContent = `${totalCaptures.toLocaleString()} captures · $${totalCost.toFixed(2)} total cloud cost`;
  } catch (e) {
    body.innerHTML = `<tr><td colspan="7" class="muted">Error: ${e.message}</td></tr>`;
  }
}

// --- Eye-State Daily Metrics (Models tab) ---
// Three SVG line charts (precision / recall / F1) with one line per class
// (eyes_open, eyes_closed). Powered by /api/eye-state-daily-metrics.
//
// Days where a class has zero ground-truth support render as a gap so
// "no signal today" doesn't masquerade as a 0.0 metric. Hover dots show
// the per-day value + support count.
const EYE_METRICS = [
  { key: 'precision', label: 'Precision' },
  { key: 'recall', label: 'Recall' },
  { key: 'f1', label: 'F1 Score' },
];
const EYE_CLASSES = [
  { key: 'eyes_open', label: 'Eyes open', color: '#f0b429' },
  { key: 'eyes_closed', label: 'Eyes closed', color: '#4a9eff' },
];

async function loadEyeStateDailyMetrics() {
  const days = document.getElementById('eye-metrics-days').value;
  const grid = document.getElementById('eye-metrics-grid');
  const note = document.getElementById('eye-metrics-note');
  try {
    const res = await fetch('/api/eye-state-daily-metrics?days=' + encodeURIComponent(days));
    const data = await res.json();
    const rows = data.rows || [];
    if (!rows.length) {
      grid.innerHTML = '<div class="muted" style="padding:12px">No labelled frames in this range.</div>';
      note.textContent = '';
      return;
    }
    const totalLabels = rows.reduce((s, r) => s + (r.total || 0), 0);
    note.textContent = `${rows.length} day${rows.length === 1 ? '' : 's'} · ${totalLabels.toLocaleString()} labelled frames`;
    grid.innerHTML = EYE_METRICS.map(m => renderEyeMetricChart(m, rows)).join('');
  } catch (e) {
    grid.innerHTML = `<div class="muted" style="padding:12px">Error: ${e.message}</div>`;
    note.textContent = '';
  }
}

function renderEyeMetricChart(metric, rows) {
  // SVG layout: 280×140 plot area inside a 320×190 viewBox.
  const W = 320, H = 190;
  const PAD = { top: 16, right: 12, bottom: 38, left: 32 };
  const plotW = W - PAD.left - PAD.right;
  const plotH = H - PAD.top - PAD.bottom;
  const n = rows.length;
  // X positions evenly spaced; if only one day, center it.
  const xAt = i => n === 1 ? PAD.left + plotW / 2 : PAD.left + (i / (n - 1)) * plotW;
  const yAt = v => PAD.top + (1 - v) * plotH;

  // Y-axis gridlines at 0, 0.5, 1.0
  let svg = `<svg viewBox="0 0 ${W} ${H}" class="eye-metric-svg" preserveAspectRatio="xMidYMid meet">`;
  for (const v of [0, 0.5, 1.0]) {
    const y = yAt(v);
    svg += `<line x1="${PAD.left}" y1="${y}" x2="${W - PAD.right}" y2="${y}" class="eye-metric-grid"/>`;
    svg += `<text x="${PAD.left - 4}" y="${y + 3}" class="eye-metric-axis" text-anchor="end">${v.toFixed(1)}</text>`;
  }

  // X-axis labels: first, middle, last (avoids crowding for 14- or 30-day ranges)
  const labelIdxs = n <= 1 ? [0] : n === 2 ? [0, 1] : [0, Math.floor((n - 1) / 2), n - 1];
  for (const i of labelIdxs) {
    const d = rows[i].date.slice(5); // MM-DD
    svg += `<text x="${xAt(i)}" y="${H - PAD.bottom + 14}" class="eye-metric-axis" text-anchor="middle">${d}</text>`;
  }

  // One polyline per class, broken by gaps where the metric is null.
  for (const cls of EYE_CLASSES) {
    let segment = [];
    const flush = () => {
      if (segment.length === 0) return;
      const pts = segment.map(([x, y]) => `${x},${y}`).join(' ');
      if (segment.length === 1) {
        // Lone point — render just the dot below.
      } else {
        svg += `<polyline points="${pts}" class="eye-metric-line" stroke="${cls.color}"/>`;
      }
      segment = [];
    };
    rows.forEach((r, i) => {
      const v = r[cls.key] && r[cls.key][metric.key];
      if (v == null) { flush(); return; }
      segment.push([xAt(i), yAt(v)]);
    });
    flush();
    // Dots with hover titles for every defined point
    rows.forEach((r, i) => {
      const cell = r[cls.key];
      const v = cell ? cell[metric.key] : null;
      if (v == null) return;
      const title = `${r.date} · ${cls.label} ${metric.label.toLowerCase()}: ${v.toFixed(3)} (n=${cell.support})`;
      svg += `<circle cx="${xAt(i)}" cy="${yAt(v)}" r="2.5" fill="${cls.color}"><title>${title}</title></circle>`;
    });
  }

  svg += `</svg>`;

  const legend = EYE_CLASSES.map(c =>
    `<span><span class="legend-dot" style="background:${c.color}"></span>${c.label}</span>`
  ).join('');

  return `
    <div class="eye-metric-card">
      <div class="eye-metric-title">${metric.label}</div>
      ${svg}
      <div class="eye-metric-legend">${legend}</div>
    </div>
  `;
}

// --- Recap tab ---
// Day-in-a-minute time-lapse. Clicking Generate POSTs to /api/recap/generate,
// which stitches the day's frames via ffmpeg and caches the MP4 by
// (date, fps, frame count). The server reuses the cache when the count
// matches, so repeat clicks are instant.
function initRecap() {
  const picker = document.getElementById('recap-date');
  const today = new Date().toLocaleDateString('en-CA', { timeZone: 'America/New_York' });
  picker.value = today;
  picker.max = today;

  const bump = (days) => {
    const d = new Date(picker.value + 'T12:00:00');
    d.setDate(d.getDate() + days);
    const next = d.toISOString().slice(0, 10);
    if (next > picker.max) return;
    picker.value = next;
  };
  document.getElementById('recap-prev').addEventListener('click', () => bump(-1));
  document.getElementById('recap-next').addEventListener('click', () => bump(1));
  document.getElementById('recap-generate').addEventListener('click', generateRecap);
}

async function generateRecap() {
  const date = document.getElementById('recap-date').value;
  const fps = parseInt(document.getElementById('recap-fps').value, 10);
  const status = document.getElementById('recap-status');
  const info = document.getElementById('recap-info');
  const video = document.getElementById('recap-video');
  const placeholder = document.getElementById('recap-placeholder');
  const btn = document.getElementById('recap-generate');

  if (!date) {
    status.textContent = 'Pick a date first.';
    return;
  }

  btn.disabled = true;
  status.textContent = 'Generating…';
  info.textContent = '';

  try {
    const resp = await fetch('/api/recap/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ date, fps }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || ('HTTP ' + resp.status));

    if (data.status === 'empty') {
      video.style.display = 'none';
      video.removeAttribute('src');
      placeholder.style.display = '';
      placeholder.textContent = `No frames captured on ${date}.`;
      status.textContent = '';
      return;
    }

    // Bust the browser cache if we just regenerated.
    const cacheBuster = data.cached ? '' : '&t=' + Date.now();
    video.src = data.video_url + cacheBuster;
    video.style.display = '';
    placeholder.style.display = 'none';

    const mb = (data.size_bytes / 1024 / 1024).toFixed(1);
    info.textContent =
      `${data.frame_count} frames · ${data.duration_sec.toFixed(1)} s @ ${data.fps} fps · ${mb} MB` +
      (data.cached ? ' · cached' : '');
    status.textContent = '';
  } catch (e) {
    status.textContent = 'Error: ' + e.message;
  } finally {
    btn.disabled = false;
  }
}

initTabs();
initTimelineNav();
initRecap();
document.getElementById('perf-range').addEventListener('change', loadMonitorStats);
document.getElementById('safety-range').addEventListener('change', loadSafetyStats);
document.getElementById('events-count').addEventListener('change', loadEvents);
document.getElementById('events-type').addEventListener('change', loadEvents);
document.getElementById('events-range').addEventListener('change', loadEvents);
document.getElementById('bassinet-days').addEventListener('change', loadBassinetChart);
document.getElementById('pipeline-history-days').addEventListener('change', loadPipelineHistory);
document.getElementById('eye-metrics-days').addEventListener('change', loadEyeStateDailyMetrics);
loadAll();
setInterval(loadAll, REFRESH_INTERVAL_SEC * 1000);
