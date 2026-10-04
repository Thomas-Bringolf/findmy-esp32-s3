/* Find My Dashboard — map + controls. */

const STATUS = {
  UNLOCKED: 0x01,
  CONFIG:   0x02,
  LOWBATT:  0x04,
  POWER:    0x08,   // OS running  -> fill
  LOGIN:    0x10,   // user logged -> outline
  NET:      0x20,
  PARITY:   0x40,
};

const COLORS = {
  fillOn:     '#16a34a',
  fillOff:    '#9aa3ad',
  outlineOn:  '#2563eb',
  outlineOff: '#6b7280',
};

const map = L.map('map').setView([35, 10], 2);
const cartoKey = window.CARTO_API_KEY || '';
L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png' +
            (cartoKey ? '?api_key=' + cartoKey : ''), {
  attribution: '&copy; OpenStreetMap &copy; CARTO',
  subdomains: 'abcd',
  maxZoom: 19,
}).addTo(map);

let markers = [];
let allReports = [];
let knownDevices = [];
let currentDevice = '';    // '' = all

const fadeInput = document.getElementById('fade');
const fadeValue = document.getElementById('fadeValue');
const deviceSel = document.getElementById('device');
const autoRefresh = document.getElementById('autorefresh');
const refreshBtn = document.getElementById('refresh');

/* Legend preview */
fixLegend();

function fixLegend() {
  const fill = document.getElementById('legendFill');
  const outline = document.getElementById('legendOutline');
  fill.style.background = COLORS.fillOn;
  fill.style.borderColor = COLORS.outlineOn;
  outline.style.background = COLORS.fillOn;
  outline.style.borderColor = COLORS.outlineOn;
}

async function api(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error('HTTP ' + res.status + ' for ' + path);
  return res.json();
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

/* ---- devices / ledger ---- */
async function loadDevices() {
  const data = await api('/api/devices');
  knownDevices = data.devices || [];
  const prev = deviceSel.value;
  deviceSel.innerHTML = '<option value="">All devices</option>'
    + knownDevices.map(d => `<option value="${esc(d.id)}">${esc(d.id)}</option>`).join('');
  if (prev) deviceSel.value = prev;
}

async function loadStatus() {
  const data = await api('/api/status');
  const rows = data.statuses || [];
  const tbody = document.getElementById('ledgerBody');
  tbody.innerHTML = rows.length ? rows.map(st => `
    <tr>
      <td>${esc(st.device_id)}</td>
      <td>${fmtAge(st.report_time)}</td>
      <td>${statusBadge(st.status)}</td>
    </tr>`).join('')
    : '<tr><td colspan="3">No reports yet</td></tr>';
}

function statusBadge(st) {
  const running = (st & STATUS.POWER) ? 'on' : 'off';
  const login = (st & STATUS.LOGIN) ? 'on' : 'off';
  return `<span class="badge ${running}">OS ${running}</span> ` +
         `<span class="badge ${login}">user ${login}</span>`;
}

/* ---- reports / map ---- */
async function loadReports() {
  const q = currentDevice ? '?device=' + encodeURIComponent(currentDevice) : '';
  const data = await api('/api/reports' + q);
  allReports = data.reports || [];
  draw();
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function draw() {
  markers.forEach(m => { try { m.remove(); } catch (e) {} });
  markers = [];

  const fadeHours = Number(fadeInput.value);
  const now = Date.now();

  for (const r of allReports) {
    if (r.latitude == null || r.longitude == null) continue;

    const t = new Date(r.report_time).getTime();
    const ageHours = (now - t) / 3600000;

    /* Opacity: 100% when fresh, 0% when older than the fade slider. */
    const opacity = fadeHours <= 0 ? 1 : Math.max(0, 1 - ageHours / fadeHours);

    const running = !!(r.status & STATUS.POWER);
    const login = !!(r.status & STATUS.LOGIN);
    const fill = running ? COLORS.fillOn : COLORS.fillOff;
    const outline = login ? COLORS.outlineOn : COLORS.outlineOff;

    const circle = L.circleMarker([r.latitude, r.longitude], {
      radius: 8,
      color: outline,
      weight: 3,
      fillColor: fill,
      fillOpacity: opacity,
      opacity: opacity,
    }).bindPopup(
      `<b>${esc(r.device_id)}</b><br>` +
      `slot ${r.slot}<br>${esc(fmtAge(r.report_time))} ago<br>` +
      `OS <b>${running ? 'running' : 'off'}</b>, user <b>${login ? 'in' : 'out'}</b>`
    );
    markers.push(circle.addTo(map));
  }
}

/* ---- events ---- */
deviceSel.addEventListener('change', () => {
  currentDevice = deviceSel.value;
  loadReports().catch(err => console.error(err));
});

fadeInput.addEventListener('input', () => {
  fadeValue.textContent = fadeInput.value + 'h';
  draw();
});

refreshBtn.addEventListener('click', async () => {
  await Promise.all([loadDevices(), loadStatus(), loadReports()]).catch(console.error);
});

setInterval(() => {
  if (autoRefresh.checked) {
    loadStatus().catch(console.error);
    loadReports().catch(console.error);
  }
}, 30000);

/* ---- init ---- */
(async function init() {
  try {
    await Promise.all([loadDevices(), loadStatus(), loadReports()]);
  } catch (e) {
    console.error('init failed', e);
    document.getElementById('ledgerBody').innerHTML =
      '<tr><td colspan="3">Backend unreachable — is it running?</td></tr>';
  }
})();
