/* Find My Dashboard — map + controls. */

const map = L.map('map').setView([35, 10], 2);
const cartoKey = window.CARTO_API_KEY || '';
L.tileLayer('https://basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png' +
            (cartoKey ? '?key=' + cartoKey : ''), {
  attribution: '&copy; OpenStreetMap &copy; CARTO',
  maxZoom: 19,
}).addTo(map);

let markers = [];
let allReports = [];
let knownDevices = [];
let currentDevice = '';    // '' = all

/* ----- Device colours ----- */
let deviceColors = {};
const PALETTE = [
  '#16a34a', '#2563eb', '#dc2626', '#ea580c',
  '#059669', '#7c3aed', '#db2777', '#be185d',
];

function getDeviceColor(deviceId) {
  if (!deviceColors[deviceId]) {
    const idx = knownDevices.findIndex(d => d.id === deviceId);
    deviceColors[deviceId] = PALETTE[idx % PALETTE.length];
  }
  return deviceColors[deviceId];
}

/* ----- fade ----- */
function parseFadeHours(val) {
  if (val === 'custom') {
    const input = document.getElementById('fadeCustomInput').value.trim();
    const re = /(\d+)([YyMmDdHhMm])/g;
    let totalHours = 0;
    let m;
    while ((m = re.exec(input)) !== null) {
      const n = parseInt(m[1], 10);
      const u = m[2].toLowerCase();
      if (u === 'y') totalHours += n * 365 * 24;
      else if (u === 'm') totalHours += n * 30 * 24;
      else if (u === 'd') totalHours += n * 24;
      else if (u === 'h') totalHours += n;
      else if (u === 'min') totalHours += n / 60;
    }
    return totalHours >= 0 ? totalHours : 168;
  }
  return parseInt(val, 10) || 168;
}

function fmtAge(iso) {
  const t = new Date(iso);
  if (isNaN(t)) return '—';
  const sec = Math.max(0, (Date.now() - t.getTime()) / 1000);
  if (sec < 60) return Math.round(sec) + 's';
  if (sec < 3600) return (sec / 60).toFixed(0) + 'm';
  if (sec < 86400) return (sec / 3600).toFixed(1) + 'h';
  return (sec / 86400).toFixed(1) + 'd';
}

/* ----- api ----- */
async function api(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error('HTTP ' + res.status + ' for ' + path);
  return res.json();
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

/* ----- devices / ledger ----- */
async function loadDevices() {
  const data = await api('/api/devices');
  knownDevices = data.devices || [];
  const prev = document.getElementById('device').value;
  const sel = document.getElementById('device');
  sel.innerHTML = '<option value="">All devices</option>'
    + knownDevices.map(d => `<option value="${esc(d.id)}">${esc(d.id)}</option>`).join('');
  if (prev) sel.value = prev;
  renderSlotPanel();
}

/* ----- manual slot sync ----- */
function findDevice(id) {
  return knownDevices.find(d => d.id === id) || null;
}

function renderSlotPanel() {
  const info = document.getElementById('slotInfo');
  const input = document.getElementById('slotInput');
  const setBtn = document.getElementById('slotSet');
  const clearBtn = document.getElementById('slotClear');
  const dev = findDevice(currentDevice);

  if (!dev) {
    info.innerHTML = '<span class="muted">Select a device to manage its slot.</span>';
    input.value = '';
    setBtn.disabled = clearBtn.disabled = true;
    return;
  }
  const ss = dev.slot_seconds || 120;
  let html = '';
  if (dev.manual_override) {
    html = `manual: <b>slot ${dev.slot}</b> (set ${esc(fmtAge(dev.override_set_at))} ago)`;
  } else if (dev.slot != null) {
    html = `stored: <b>slot ${dev.slot}</b>`;
  } else {
    html = '<span class="muted">no slot known yet</span>';
  }
  if (dev.estimated_slot != null) {
    const src = dev.estimate_source === 'report' ? 'from last report' : 'from slot sync';
    html += ` — beacon should be at ~<b>${dev.estimated_slot}</b>` +
            ` <span class="muted">(${ss}s slots, ${src})</span>`;
  }
  info.innerHTML = html;

  input.value = dev.estimated_slot != null ? dev.estimated_slot : '';
  setBtn.disabled = false;
  clearBtn.disabled = !dev.manual_override;
}

async function setSlot() {
  const dev = findDevice(currentDevice);
  if (!dev) return;
  const input = document.getElementById('slotInput');
  const slot = parseInt(input.value, 10);
  if (isNaN(slot) || slot < 0) {
    alert('Enter a valid slot number (>= 0).');
    return;
  }
  if (!confirm(`Set ${dev.id} to slot ${slot}, valid from now on?`)) return;
  const res = await fetch('/api/slot', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ device: dev.id, slot }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    alert('Failed to set slot: ' + (err.error || res.status));
    return;
  }
  await loadDevices();
}

async function clearSlot() {
  const dev = findDevice(currentDevice);
  if (!dev) return;
  if (!confirm(`Remove the manual slot override for ${dev.id}?`)) return;
  const res = await fetch('/api/slot?device=' + encodeURIComponent(dev.id),
                         { method: 'DELETE' });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    alert('Failed to clear override: ' + (err.error || res.status));
    return;
  }
  await loadDevices();
}

/* ----- status badge (kept for ledger) ----- */
function statusBadge(st) {
  const running = st & 0x08;
  const login = st & 0x10;
  return `<span class="badge ${running ? 'on' : 'off'}">OS ${running ? 'on' : 'off'}</span> ` +
         `<span class="badge ${login ? 'on' : 'off'}">user ${login ? 'logged in' : 'logged out'}</span>`;
}

/* ----- status ledger ----- */
async function loadStatus() {
  const data = await api('/api/status');
  const rows = data.statuses || [];
  const tbody = document.getElementById('ledgerBody');
  if (rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="3">No status yet</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(r => {
    const st = r.status || 0;
    return `<tr>
      <td><span class="dot" style="background:${getDeviceColor(r.device_id)}"></span>${esc(r.device_id)}</td>
      <td>${esc(fmtAge(r.report_time))} ago</td>
      <td>${statusBadge(st)}</td>
    </tr>`;
  }).join('');
}

/* ----- reports / map ----- */
async function loadReports() {
  const q = currentDevice ? '?device=' + encodeURIComponent(currentDevice) : '';
  const data = await api('/api/reports' + q);
  allReports = data.reports || [];
  draw();
}

/* ----- history ----- */
async function loadHistory() {
  const deviceId = currentDevice || '';
  const data = await api('/api/history' + (deviceId ? '?device=' + encodeURIComponent(deviceId) : ''));
  const historyDiv = document.getElementById('history');
  if (!data || !data.history) {
    historyDiv.innerHTML = '<p>No history yet</p>';
    return;
  }

  // Render for the selected device (or all if 'All devices')
  const entries = data.history[deviceId] ? data.history[deviceId].events : [];
  // Also show if all devices: combine
  let html = '';
  if (currentDevice === '') {
    // Show all devices' events, grouped
    const allEvents = [];
    for (const did of Object.keys(data.history)) {
      const evts = data.history[did].events || [];
      for (const e of evts) allEvents.push({ time: e.time, changes: e.changes, device: did });
    }
    // Sort newest first
    allEvents.sort((a, b) => new Date(b.time) - new Date(a.time));
    for (const e of allEvents.slice(0, 20)) {
      const age = fmtAge(e.time);
      const changesTxt = e.changes.join(', ') || '—';
      html += `<p><span class="time">${age} ago</span> <span class="changes">${changesTxt}</span> <span class="device">device ${esc(e.device)}</span></p>`;
    }
    if (allEvents.length === 0) html = '<p>No state changes yet</p>';
    if (allEvents.length > 20) html += '<p><small>… showing newest 20</small></p>';
  } else {
    const evts = entries || [];
    evts.sort((a, b) => new Date(b.time) - new Date(a.time));
    for (const e of evts.slice(0, 20)) {
      const age = fmtAge(e.time);
      const changesTxt = e.changes.join(', ') || '—';
      html += `<p><span class="time">${age} ago</span> <span class="changes">${changesTxt}</span></p>`;
    }
    if (evts.length === 0) html = '<p>No state changes yet</p>';
    if (evts.length > 20) html += '<p><small>… showing newest 20</small></p>';
  }
  historyDiv.innerHTML = html;
}

/* ----- draw map ----- */

/* Status bits as reported at the moment a position was recorded. */
const STATE_BITS = [
  [0x01, 'unlocked'],
  [0x02, 'config mode'],
  [0x04, 'low battery'],
  [0x08, 'power on'],
  [0x10, 'user logged in'],
  [0x20, 'network on'],
  [0x40, 'parity'],
];

function stateText(st) {
  const on = STATE_BITS.filter(([bit]) => (st || 0) & bit).map(([, txt]) => txt);
  return on.length ? on.join(', ') : '—';
}

function popupHtml(r) {
  const lat = Number(r.latitude).toFixed(6);
  const lng = Number(r.longitude).toFixed(6);
  const gmaps = `https://www.google.com/maps?q=${lat},${lng}`;
  const amaps = `https://maps.apple.com/?ll=${lat},${lng}&q=${lat},${lng}`;
  return `<b>${esc(r.device_id)}</b><br>` +
    `slot ${r.slot} · ${esc(fmtAge(r.report_time))} ago<br>` +
    `${lat}, ${lng}<br>` +
    `<a href="${gmaps}" target="_blank" rel="noopener">Google Maps</a> · ` +
    `<a href="${amaps}" target="_blank" rel="noopener">Apple Maps</a><br>` +
    `<span class="muted">state then:</span> ${esc(stateText(r.status))}`;
}

/*
 * Zero-phase low-pass on the raw track: an EMA run forward over time and
 * again backwards, so it lags neither left nor right. alpha (0..1, lower =
 * smoother) and passes control how much smoothing is applied.
 */
function smoothTrack(points, alpha = 0.6, passes = 1) {
  const pts = points.map(p => ({ lat: p.lat, lng: p.lng }));
  for (let k = 0; k < passes; k++) {
    for (let i = 1; i < pts.length; i++) {
      pts[i].lat = alpha * pts[i].lat + (1 - alpha) * pts[i - 1].lat;
      pts[i].lng = alpha * pts[i].lng + (1 - alpha) * pts[i - 1].lng;
    }
    for (let i = pts.length - 2; i >= 0; i--) {
      pts[i].lat = alpha * pts[i].lat + (1 - alpha) * pts[i + 1].lat;
      pts[i].lng = alpha * pts[i].lng + (1 - alpha) * pts[i + 1].lng;
    }
  }
  return pts;
}

function draw() {
  markers.forEach(m => { try { m.remove(); } catch (e) {} });
  markers = [];

  const fadeVal = document.querySelector('input[name="fade"]:checked').value;
  const fadeHours = parseFadeHours(fadeVal);
  const now = Date.now();

  /* group by device, oldest -> newest, for the movement line */
  const byDevice = {};
  for (const r of allReports) {
    if (r.latitude == null || r.longitude == null) continue;
    (byDevice[r.device_id] = byDevice[r.device_id] || []).push(r);
  }

  for (const [deviceId, reports] of Object.entries(byDevice)) {
    const color = getDeviceColor(deviceId);
    reports.sort((a, b) => new Date(a.report_time) - new Date(b.report_time));

    /* movement estimate: dots connected, then low-pass filtered.
       The line only spans the selected time frame and fades out for older
       parts, segment by segment, with the same gamma curve as the dots. */
    const cutoffMs = fadeHours > 0 ? fadeHours * 3600000 : Infinity;
    const track = reports
      .filter(r => now - new Date(r.report_time).getTime() <= cutoffMs)
      .map(r => ({ lat: r.latitude, lng: r.longitude,
                   t: new Date(r.report_time).getTime() }));
    if (track.length >= 2) {
      const smooth = smoothTrack(track);
      for (let i = 1; i < smooth.length; i++) {
        const midAgeHours = (now - (track[i - 1].t + track[i].t) / 2) / 3600000;
        const x = fadeHours > 0
          ? Math.min(1, Math.max(0, midAgeHours / fadeHours)) : 0;
        const opacity = 0.45 * Math.pow(1 - x, 2);
        if (opacity <= 0.01) continue;
        markers.push(L.polyline(
          [[smooth[i - 1].lat, smooth[i - 1].lng],
           [smooth[i].lat, smooth[i].lng]], {
          color: color,
          weight: 2,
          opacity: opacity,
        }).addTo(map));
      }
    }

    for (const r of reports) {
      const t = new Date(r.report_time).getTime();
      const ageHours = (now - t) / 3600000;
      const x = fadeHours > 0 ? Math.min(1, Math.max(0, ageHours / fadeHours)) : 0;
      /* gamma curve instead of linear: mid-age dots fade earlier and the
         ramp stays perceptible across the whole range, not just near the end */
      const opacity = Math.pow(1 - x, 2);

      const dot = L.circleMarker([r.latitude, r.longitude], {
        radius: 7,
        stroke: false,          // no outline
        fillColor: color,
        fillOpacity: opacity,
      }).bindPopup(popupHtml(r));
      markers.push(dot.addTo(map));
    }
  }
}

/* ----- events ----- */
document.getElementById('device').addEventListener('change', () => {
  currentDevice = document.getElementById('device').value;
  renderSlotPanel();
  loadReports().catch(err => console.error(err));
  loadHistory().catch(err => console.error(err));
});

document.getElementById('slotSet').addEventListener('click', () => setSlot().catch(console.error));
document.getElementById('slotClear').addEventListener('click', () => clearSlot().catch(console.error));

document.querySelectorAll('input[name="fade"]').forEach(radio => {
  radio.addEventListener('change', () => {
    if (radio.value === 'custom') {
      document.getElementById('fadeCustomInput').style.display = 'inline';
      radio.checked = true;
    } else {
      document.getElementById('fadeCustomInput').style.display = 'none';
      document.getElementById('fadeCustomInput').value = '';
    }
    const fadeHours = parseFadeHours(radio.value);
    document.getElementById('fadeValue').textContent = radio.value === 'custom'
      ? (isNaN(parseFadeHours(radio.value)) ? '168h' : parseFadeHours(radio.value) + 'h')
      : radio.value;
    draw();
  });
});

document.getElementById('fadeCustomInput').addEventListener('input', () => {
  const val = document.getElementById('fadeCustomInput').value;
  document.getElementById('fadeValue').textContent = val + 'h';
  draw();
});

document.getElementById('refresh').addEventListener('click', async () => {
  await Promise.all([loadDevices(), loadStatus(), loadReports(), loadHistory()]).catch(console.error);
});

setInterval(() => {
  if (document.getElementById('autorefresh').checked) {
    loadStatus().catch(console.error);
    loadReports().catch(console.error);
    loadHistory().catch(console.error);
  }
}, 30000);

/* ----- init ----- */
(async function init() {
  try {
    await Promise.all([loadDevices(), loadStatus(), loadReports(), loadHistory()]);
  } catch (e) {
    console.error('init failed', e);
    document.getElementById('ledgerBody').innerHTML =
      '<tr><td colspan="3">Backend unreachable — is it running?</td></tr>';
  }
})();