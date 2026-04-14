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

    let html = '';
    for (const d of data.days) {
      const total = d.inHours + d.outHours;
      const stackPct = total > 0 ? (total / maxHours * 100) : 0;
      const inPct = total > 0 ? (d.inHours / total * 100) : 0;
      const outPct = 100 - inPct;

      // Format date as "Mon 4/7"
      const dt = new Date(d.date + 'T12:00:00');
      const dayName = dt.toLocaleDateString('en-US', { weekday: 'short' });
      const monthDay = (dt.getMonth() + 1) + '/' + dt.getDate();

      html += '<div class="bassinet-bar-group">';
      html += '<div class="bassinet-bar-stack" style="height:100%">';
      html += '<div style="flex:' + (100 - stackPct) + '"></div>'; // spacer
      if (outPct > 0 && d.outHours > 0) {
        html += '<div class="bassinet-bar-seg out" style="flex:' + (stackPct * outPct / 100) + '" title="Out: ' + d.outHours + 'h">'
          + (d.outHours >= 1 ? d.outHours + 'h' : '') + '</div>';
      }
      if (inPct > 0 && d.inHours > 0) {
        html += '<div class="bassinet-bar-seg in" style="flex:' + (stackPct * inPct / 100) + '" title="In: ' + d.inHours + 'h (' + d.inPct + '%)">'
          + (d.inHours >= 1 ? d.inHours + 'h' : '') + '</div>';
      }
      html += '</div>';
      html += '<div class="bassinet-bar-label">' + dayName + '<br>' + monthDay + '</div>';
      html += '</div>';
    }

    html += '</div>';
    // Legend
    html += '<div class="bassinet-chart-legend">';
    html += '<span><span class="legend-dot" style="background:var(--accent-blue)"></span> In Bassinet</span>';
    html += '<span><span class="legend-dot" style="background:rgba(255,152,0,0.5)"></span> Out of Bassinet</span>';
    html += '</div>';

    chartEl.innerHTML = html;
  } catch (e) {
    console.error('Bassinet chart error:', e);
  }
}


// ---------------------------------------------------------------------------
// Pending Corrections
// ---------------------------------------------------------------------------
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

      // Corrected label
      const tdCorr = document.createElement('td');
      tdCorr.textContent = friendlyLabel(c.correctedEyeState || c.correctedState);
      tdCorr.className = 'corr-label corr-label-new';
      tr.appendChild(tdCorr);

      // BIRDEYE prediction — eye-state direct from shadow_birdeye_eye column
      const tdBirdeye = document.createElement('td');
      if (c.shadowBirdeyeEye) {
        const labelMap = { eyes_open: 'Eyes Open', eyes_closed: 'Eyes Closed' };
        tdBirdeye.textContent = labelMap[c.shadowBirdeyeEye] || c.shadowBirdeyeEye;
        const agreed = c.correctedEyeState === c.shadowBirdeyeEye;
        tdBirdeye.className = 'corr-label' + (agreed ? ' corr-agree' : ' corr-disagree');
      } else if (c.shadowBirdeyePresent === 0) {
        tdBirdeye.textContent = 'Not Present';
        const agreed = c.correctedEyeState === 'not_in_bassinet';
        tdBirdeye.className = 'corr-label' + (agreed ? ' corr-agree' : ' corr-disagree');
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

function delta(curr, prev, suffix, higherIsBetter) {
  if (prev == null || curr == null) return '';
  const diff = curr - prev;
  if (Math.abs(diff) < 0.001) return '';
  const sign = diff > 0 ? '+' : '';
  const good = higherIsBetter ? diff > 0 : diff < 0;
  const color = good ? 'var(--accent-green)' : 'var(--accent-red)';
  return ' <span style="font-size:0.7rem;color:' + color + '">' + sign + (diff * 100).toFixed(1) + suffix + '</span>';
}

function renderFaceDetectionColumn() {
  const el = document.getElementById('classifier-face');
  if (!el) return;

  const face = safetyData ? safetyData.faceDetection : null;
  if (!face || face.total === 0) {
    el.innerHTML = '<div class="safety-empty">No data yet — populates as new frames arrive with face detection.</div>';
    return;
  }

  let html = '';

  // --- Production headlines ---
  html += '<div class="safety-source-label">Production (shadow)</div>';
  html += '<div class="safety-headline">';

  // Detection rate
  html += '<div class="safety-headline-row" title="% of baby-present frames where a face was detected">';
  html += '<span class="safety-headline-label">Detection Rate</span>';
  html += '<span class="safety-headline-value ' + _safetyClass(face.detectionRate, [0.50, 0.75]) + '">'
    + Math.round(face.detectionRate * 100) + '%</span></div>';

  // Fallback rate
  html += '<div class="safety-headline-row" title="% of baby-present frames where face detection failed → cloud API fallback">';
  html += '<span class="safety-headline-label">Fallback Rate</span>';
  html += '<span class="safety-headline-value">' + Math.round(face.fallbackRate * 100) + '%</span></div>';

  // Frame counts
  html += '<div class="safety-headline-row">';
  html += '<span class="safety-headline-label">Frames</span>';
  html += '<span class="safety-headline-value">' + face.detected + ' / ' + face.total + '</span></div>';
  html += '</div>';

  // --- IoU vs dashboard-drawn corrections ---
  // This is the "how tight are the model's bboxes" metric, measured on
  // frames where the user has drawn a corrected bbox. A corrected bbox is
  // treated as ground truth. Two scopes: within the selected time range
  // (tracks current model behavior) and all-time (larger N, may mix model
  // versions). We prefer the windowed one when it has enough pairs and
  // fall back to allTime otherwise.
  if (face.iou) {
    const iouWindowed = face.iou.windowed || {n: 0};
    const iouAll = face.iou.allTime || {n: 0};
    const useWindowed = iouWindowed.n >= 10;
    const iou = useWindowed ? iouWindowed : iouAll;
    const scopeLabel = useWindowed ? 'in window' : 'all time';

    html += '<div class="safety-source-label" title="IoU between BIRDEYE predicted bbox and your dashboard-drawn corrected bbox. Corrected bbox is treated as ground truth.">IoU vs corrections <span style="font-weight:400;color:var(--text-muted);font-size:0.75rem">(' + scopeLabel + ')</span></div>';

    if (iou.n === 0) {
      html += '<div class="safety-empty" style="padding:8px 0">No corrected bboxes yet. Use the face-box draw tool on a frame to start populating this.</div>';
    } else {
      html += '<div class="train-details">';
      html += '<div class="train-row"><span title="Mean Intersection-over-Union across all frames where you corrected the face bbox">Mean IoU</span><span class="train-val ' + _safetyClass(iou.mean, [0.40, 0.65]) + '">'
        + (iou.mean * 100).toFixed(1) + '%</span></div>';
      html += '<div class="train-row"><span title="Median (p50) IoU — less sensitive to outliers than mean">Median</span><span class="train-val">'
        + (iou.p50 * 100).toFixed(1) + '%</span></div>';
      html += '<div class="train-row"><span title="Worst-decile IoU — the 10% of frames where the model is furthest from your correction">p10 (worst tail)</span><span class="train-val">'
        + (iou.p10 * 100).toFixed(1) + '%</span></div>';

      const over50Pct = iou.n > 0 ? iou.over50 / iou.n : 0;
      const over75Pct = iou.n > 0 ? iou.over75 / iou.n : 0;
      html += '<div class="train-row"><span title="Fraction of frames where IoU ≥ 0.5 — conventional usable-detection threshold">Usable (≥0.5)</span><span class="train-val ' + _safetyClass(over50Pct, [0.70, 0.90]) + '">'
        + iou.over50 + ' / ' + iou.n + ' (' + Math.round(over50Pct * 100) + '%)</span></div>';
      html += '<div class="train-row"><span title="Fraction of frames where IoU ≥ 0.75 — tight enough for a reliable downstream eye-state crop">Tight (≥0.75)</span><span class="train-val ' + _safetyClass(over75Pct, [0.40, 0.75]) + '">'
        + iou.over75 + ' / ' + iou.n + ' (' + Math.round(over75Pct * 100) + '%)</span></div>';
      html += '</div>';
      // Footnote: if we fell back to allTime, show both counts so it's
      // obvious that the window is insufficient rather than the model is bad.
      if (!useWindowed && iouAll.n > 0) {
        html += '<div style="font-size:0.7rem;color:var(--text-muted);margin-top:4px">'
          + '(only ' + iouWindowed.n + ' corrected bboxes in the current range — showing all-time aggregate instead)</div>';
      }
    }
  }

  // --- Bbox impact on downstream eye-state ---
  // Does running eye-state on the corrected bbox produce a better answer
  // than running it on BIRDEYE's predicted bbox? Same model, two crops.
  // Computed offline by scripts/bbox_impact.py. The per-class split is
  // the part worth reading — the aggregate can hide opposite deltas per
  // class (which is exactly what happened on the first run of this).
  if (face.bboxImpact && face.bboxImpact.count > 0) {
    const bi = face.bboxImpact;
    const predPct = Math.round(bi.accuracyOnPredicted * 100);
    const corrPct = Math.round(bi.accuracyOnCorrected * 100);
    const deltaPts = Math.round(bi.delta * 1000) / 10; // one decimal
    const deltaSign = deltaPts >= 0 ? '+' : '';
    const deltaColor = deltaPts > 0.5 ? 'var(--accent-green)'
                      : deltaPts < -0.5 ? 'var(--accent-red)'
                      : 'var(--text-muted)';
    const flipPct = Math.round(bi.flipRate * 100);

    html += '<div class="safety-source-label" title="Eye-state accuracy when run on BIRDEYE\'s predicted bbox vs your corrected bbox. Same eye-state model, two crops. Computed by scripts/bbox_impact.py.">Bbox impact on eye-state</div>';
    html += '<div class="train-details">';
    html += '<div class="train-row"><span title="Eye-state accuracy when the classifier reads from BIRDEYE\'s predicted bbox crop">On predicted bbox</span><span class="train-val">'
      + predPct + '% (' + Math.round(bi.accuracyOnPredicted * bi.count) + '/' + bi.count + ')</span></div>';
    html += '<div class="train-row"><span title="Eye-state accuracy when the classifier reads from your dashboard-drawn corrected bbox crop">On corrected bbox</span><span class="train-val">'
      + corrPct + '% (' + Math.round(bi.accuracyOnCorrected * bi.count) + '/' + bi.count + ')</span></div>';
    html += '<div class="train-row"><span title="Accuracy delta: positive means corrected bbox helped, negative means it hurt">Δ (corrected − predicted)</span><span class="train-val" style="color:' + deltaColor + ';font-weight:600">'
      + deltaSign + deltaPts.toFixed(1) + ' pts</span></div>';
    html += '<div class="train-row"><span title="Fraction of frames where the eye-state prediction changed between the two bboxes — a measure of how much the bbox geometry matters regardless of which one is right">Flip rate</span><span class="train-val">'
      + flipPct + '%</span></div>';
    html += '</div>';

    // Per-class breakdown — this is the load-bearing part of the whole
    // analysis. The aggregate is easy to misread; the per-class view is
    // where the real signal lives.
    if (bi.perClass) {
      html += '<div class="safety-source-label" style="margin-top:10px;font-size:0.7rem">Per-class (this is the number to read)</div>';
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
          + Math.round(pc.accuracyOnCorrected * 100) + '% '
          + '<span style="font-weight:600">' + clsSign + clsDelta.toFixed(1) + '</span></span></div>';
      }
      html += '</div>';
    }

    // Staleness + how to refresh
    const ranAt = bi.ranAt ? new Date(bi.ranAt).toLocaleString('en-US', {
      timeZone: 'America/New_York', month: 'short', day: 'numeric',
      hour: 'numeric', minute: '2-digit', hour12: true
    }) : '?';
    const versionMatches = bi.modelVersion === (safetyData && safetyData.deployedVersion);
    const staleWarning = !versionMatches
      ? '<span style="color:var(--accent-red);font-weight:600"> · STALE (run on ' + bi.modelVersion + ')</span>'
      : '';
    html += '<div style="font-size:0.7rem;color:var(--text-muted);margin-top:4px">ran ' + ranAt + staleWarning + ' · refresh: <code>python scripts/bbox_impact.py --force</code></div>';
  }

  // --- Confidence distribution ---
  if (face.confidence) {
    const c = face.confidence;
    html += '<div class="safety-source-label">Confidence Distribution</div>';
    html += '<div class="train-details">';
    html += '<div class="train-row"><span>Avg</span><span class="train-val">' + Math.round(c.avg * 100) + '%</span></div>';
    html += '<div class="train-row"><span>Min</span><span class="train-val">' + Math.round(c.min * 100) + '%</span></div>';
    html += '<div class="train-row"><span>Median</span><span class="train-val">' + Math.round(c.p50 * 100) + '%</span></div>';
    html += '<div class="train-row"><span>Max</span><span class="train-val">' + Math.round(c.max * 100) + '%</span></div>';
    html += '</div>';
  }

  // --- Training validation metrics ---
  const faceMetrics = trainingData && trainingData.lastMetrics
    ? trainingData.lastMetrics.face_detect : null;
  if (faceMetrics) {
    html += '<div class="safety-source-label" style="margin-top:14px">Training Validation</div>';
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
      html += '<div class="train-row"><span title="Mean Intersection-over-Union on positive validation samples (face present)">Mean IoU (val)</span><span class="train-val">'
        + (faceMetrics.mean_iou * 100).toFixed(1) + '%</span></div>';
    }
    if (faceMetrics.conf_accuracy != null) {
      html += '<div class="train-row"><span title="Binary accuracy: correctly predicting face present vs absent (on val set)">Conf Accuracy (val)</span><span class="train-val">'
        + (faceMetrics.conf_accuracy * 100).toFixed(1) + '%</span></div>';
    }
    // Held-out test metrics — populated by future training runs
    if (faceMetrics.test_mean_iou != null) {
      html += '<div class="train-row"><span title="Mean IoU on held-out test set — not used for model selection, so this is the honest generalization number">Mean IoU (test)</span><span class="train-val">'
        + (faceMetrics.test_mean_iou * 100).toFixed(1) + '%</span></div>';
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
      html += '<div class="train-row"><span title="Combined SmoothL1 bbox + BCE confidence loss on validation set">Val loss</span><span class="train-val">'
        + faceMetrics.val_loss + '</span></div>';
    }
    html += '</div>';
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
  let html = '';

  // --- vs Corrections (ground truth) ---
  const safety = safetyData ? (isPresence ? safetyData.presence : safetyData.eyeState) : null;

  if (safety) {
    const bird = safety.birdeyeVsGT || {};
    const cloud = safety.cloudVsGT || {};
    const gt = safetyData.groundTruth || {};
    const macroThresh = isPresence ? [0.90, 0.97] : [0.60, 0.85];
    const accThresh = isPresence ? [0.90, 0.97] : [0.75, 0.90];
    const hasBird = bird.total > 0;
    const hasCloud = cloud.total > 0;

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
    }
    if (hasCloud) {
      html += '<div class="safety-headline-row" title="Cloud API macro F1 against reviewed + corrected ground truth"><span class="safety-headline-label">Cloud API Macro F1</span>';
      html += '<span class="safety-headline-value ' + _safetyClass(cloud.macroF1, macroThresh) + '">'
        + Math.round(cloud.macroF1 * 100) + '% <span style="font-size:0.7rem;color:var(--text-dim)">(' + cloud.total + ')</span></span></div>';
    }
    if (hasBird) {
      html += '<div class="safety-headline-row" title="BIRDEYE accuracy against ground truth"><span class="safety-headline-label">BIRDEYE Accuracy</span>';
      html += '<span class="safety-headline-value ' + _safetyClass(bird.accuracy, accThresh) + '">'
        + Math.round(bird.accuracy * 100) + '%</span></div>';
    }
    if (hasCloud) {
      html += '<div class="safety-headline-row" title="Cloud API accuracy against ground truth"><span class="safety-headline-label">Cloud API Accuracy</span>';
      html += '<span class="safety-headline-value ' + _safetyClass(cloud.accuracy, accThresh) + '">'
        + Math.round(cloud.accuracy * 100) + '%</span></div>';
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

    // Cloud API per-class P/R/F1 + collapsible confusion matrix
    if (hasCloud && cloud.confusion) {
      html += '<div class="safety-source-label">Cloud API vs Ground Truth</div>';
      html += _renderPerClass(cloud, classes);
      html += '<details class="cm-details"><summary class="cm-toggle">Confusion matrix</summary>';
      html += _renderConfusion(cloud, classes);
      html += '</details>';
    }

    if (!hasBird && !hasCloud) {
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
    html += '<div class="safety-source-label" style="margin-top:14px">Training Validation</div>';
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
  }

  el.innerHTML = html;
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
  ]);
  document.getElementById('footer-refresh').textContent =
    'Last refreshed: ' + new Date().toLocaleTimeString('en-US', { timeZone: 'America/New_York' });
}

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
  // Pick initial tab: URL hash > localStorage > default
  let initial = 'monitor';
  const fromHash = (window.location.hash || '').replace('#', '');
  if (validTabs.includes(fromHash)) {
    initial = fromHash;
  } else {
    try {
      const stored = localStorage.getItem('bilbo:tab');
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

initTabs();
initTimelineNav();
document.getElementById('perf-range').addEventListener('change', loadMonitorStats);
document.getElementById('safety-range').addEventListener('change', loadSafetyStats);
document.getElementById('events-count').addEventListener('change', loadEvents);
document.getElementById('bassinet-days').addEventListener('change', loadBassinetChart);
loadAll();
setInterval(loadAll, REFRESH_INTERVAL_SEC * 1000);
