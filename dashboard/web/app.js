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
    ({'&':'&','<':'<','>':'>','"':'"',"'":'''}[c]));
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
}

/* ----- status badge (kept for ledger) ----- */
function statusBadge(st) {
  const running = !!(st & 0x08);
  const login = !!(st & 0x10);
  return `<span class="badge ${running}">OS ${running}</span> ` +
         `<span class="badge ${login}">user ${login}</span>`;
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
function draw() {
  markers.forEach(m => { try { m.remove(); } catch (e) {} });
  markers = [];

  const fadeVal = document.querySelector('input[name="fade"]:checked').value;
  const fadeHours = parseFadeHours(fadeVal);
  const now = Date.now();

  for (const r of allReports) {
    if (r.latitude == null || r.longitude == null) continue;

    const t = new Date(r.report_time).getTime();
    const ageHours = (now - t) / 3600000;

    /* Opacity: 100% when fresh, 0% when older than fade threshold. */
    const opacity = fadeHours <= 0 ? 1 : Math.max(0, 1 - ageHours / fadeHours);

    const deviceId = r.device_id;
    const color = getDeviceColor(deviceId);

    const circle = L.circleMarker([r.latitude, r.longitude], {
      radius: 8,
      color: color,
      weight: 3,
      fillColor: color,
      fillOpacity: opacity,
      opacity: opacity,
    }).bindPopup(
      `<b>${esc(r.device_id)}</b><br>` +
      `slot ${r.slot}<br>${esc(fmtAge(r.report_time))} ago`
    );
    markers.push(circle.addTo(map));
  }
}

/* ----- events ----- */
document.getElementById('device').addEventListener('change', () => {
  currentDevice = document.getElementById('device').value;
  loadReports().catch(err => console.error(err));
  loadHistory().catch(err => console.error(err));
});

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

document.getElementById('refreshBtn').addEventListener('click', async () => {
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