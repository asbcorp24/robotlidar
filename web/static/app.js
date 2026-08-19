const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

let currentMaps = [];
let selectedMap = null;
let lastSettings = null;
let driveTimer = null;
let toastTimer = null;

async function request(url, options = {}) {
  const response = await fetch(url, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  let data = {};
  try { data = await response.json(); } catch (_) {}
  if (!response.ok) throw new Error(data.detail || data.message || `HTTP ${response.status}`);
  return data;
}

function toast(message, type = '') {
  const element = $('#toast');
  element.textContent = message;
  element.className = `toast show ${type}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { element.className = 'toast'; }, 3200);
}

function modeLabel(mode) {
  return { stopped: 'Остановлено', mapping: 'Картографирование', navigation: 'Автономный проезд', error: 'Ошибка запуска' }[mode] || mode;
}

function sensor(id, data) {
  const dot = $(`#${id}Dot`);
  const age = $(`#${id}Age`);
  if (!dot || !age) return;
  dot.className = `dot ${data?.online ? 'online' : 'offline'}`;
  age.textContent = data?.online ? `данные ${data.age_sec ?? 0} с назад` : 'нет данных';
}

function finiteOrNull(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function renderUltrasonic(us = {}, sensorState = {}) {
  const online = !!sensorState?.online;
  const valid = online && !!us.valid;
  const distance = finiteOrNull(us.distance_cm);
  const stop = !!us.stop;
  const emergency = !!us.emergency;
  const near = !!us.near;

  $('#ultrasonicDistance').textContent = valid && distance != null ? distance.toFixed(1) : '—';
  $('#ultrasonicValid').textContent = valid ? 'OK' : (online ? 'нет эха' : 'нет связи');
  $('#ultrasonicValid').className = valid ? 'value-ok' : 'value-warn';

  let zone = 'CLEAR';
  if (emergency) zone = 'EMERGENCY';
  else if (stop) zone = 'STOP';
  else if (near) zone = 'NEAR';
  else if (!valid) zone = '—';
  $('#ultrasonicZone').textContent = zone;
  $('#ultrasonicZone').className = (stop || emergency) ? 'value-warn' : (valid ? 'value-ok' : '');

  $('#ultrasonicStop').textContent = stop ? 'ДА' : 'нет';
  $('#ultrasonicStop').className = stop ? 'value-warn' : 'value-ok';
  $('#ultrasonicEmergency').textContent = emergency ? 'ДА' : 'нет';
  $('#ultrasonicEmergency').className = emergency ? 'value-warn' : 'value-ok';

  if (!online) $('#ultrasonicSummary').textContent = 'RCWL: нет связи';
  else if (!valid) $('#ultrasonicSummary').textContent = 'RCWL: нет эха';
  else $('#ultrasonicSummary').textContent = `RCWL: ${distance.toFixed(1)} см / ${zone}`;
}

function gpsReasonLabel(reason) {
  if (!reason) return 'Ожидание данных GPS';
  if (reason === 'accepted') return 'Координаты приняты. GPS используется только как слабая дополнительная коррекция.';
  if (reason === 'no_fix') return 'Нет спутникового фикса. Основная навигация продолжает работать без GPS.';
  if (reason === 'no_coordinates') return 'GPS передаёт NMEA, но координаты ещё не определены.';
  if (reason === 'no_hdop') return 'Нет оценки точности HDOP.';
  if (reason.startsWith('few_satellites:')) return `Недостаточно спутников: ${reason.split(':')[1]}.`;
  if (reason.startsWith('hdop:')) return `Низкая точность GPS, HDOP ${reason.split(':')[1]}.`;
  if (reason.startsWith('jump:')) return `Отброшен скачок координат ${reason.split(':')[1]}.`;
  return reason;
}

function renderGps(gps = {}) {
  const latitude = finiteOrNull(gps.latitude);
  const longitude = finiteOrNull(gps.longitude);
  const hdop = finiteOrNull(gps.hdop);
  const speed = finiteOrNull(gps.speed_mps);
  const course = finiteOrNull(gps.course_deg);
  const used = Number(gps.satellites_used || 0);
  const visible = Number(gps.satellites_visible || 0);
  $('#gpsFix').textContent = gps.fix_valid ? 'есть' : 'нет';
  $('#gpsFix').className = gps.fix_valid ? 'value-ok' : 'value-warn';
  $('#gpsLatitude').textContent = latitude == null ? '—' : latitude.toFixed(7);
  $('#gpsLongitude').textContent = longitude == null ? '—' : longitude.toFixed(7);
  $('#gpsSatellites').textContent = visible > used ? `${used} / ${visible}` : String(used);
  $('#gpsHdop').textContent = hdop == null ? '—' : hdop.toFixed(2);
  $('#gpsSpeed').textContent = speed == null ? '—' : speed.toFixed(2);
  $('#gpsCourse').textContent = course == null ? '—' : course.toFixed(1);
  let assist = 'выключена';
  if (gps.assist_enabled) assist = gps.assist_ready ? 'активна' : (gps.alignment_state || 'ожидание');
  $('#gpsAssist').textContent = assist;
  $('#gpsAssist').className = gps.assist_ready ? 'value-ok' : 'value-warn';
  $('#gpsReason').textContent = gpsReasonLabel(gps.reject_reason);
}

function renderStatus(data) {
  $('#connectionBadge').textContent = 'Raspberry Pi подключена';
  $('#connectionBadge').className = 'connection online';
  const runtime = data.runtime || {};
  const ros = data.ros || {};
  $('#modeTitle').textContent = modeLabel(runtime.mode || 'stopped');
  $('#poseX').textContent = Number(ros.pose?.x || 0).toFixed(3);
  $('#poseY').textContent = Number(ros.pose?.y || 0).toFixed(3);
  $('#poseYaw').textContent = (Number(ros.pose?.yaw || 0) * 180 / Math.PI).toFixed(1);
  $('#routeState').textContent = ros.route_recording ? 'запись' : (ros.route_player_state || '—');
  sensor('lidar', ros.sensors?.lidar);
  sensor('imu', ros.sensors?.imu);
  sensor('hall', ros.sensors?.wheel_odom);
  sensor('odom', ros.sensors?.odom);
  sensor('gps', ros.sensors?.gps);
  sensor('ultrasonic', ros.sensors?.ultrasonic);
  renderUltrasonic(ros.ultrasonic || {}, ros.sensors?.ultrasonic || {});
  renderGps(ros.gps || data.gps || {});
  $('#logs').textContent = (runtime.logs || []).join('\n') || 'Журнал пока пуст.';
  $('#logs').scrollTop = $('#logs').scrollHeight;
  lastSettings = data.settings || lastSettings;
  if (lastSettings) {
    $('#autoStart').checked = !!lastSettings.auto_start;
    $('#startupMode').value = lastSettings.startup_mode || 'navigation';
    selectedMap = selectedMap || lastSettings.default_map;
  }
}

async function refreshStatus() {
  try { renderStatus(await request('/api/status')); }
  catch (error) {
    $('#connectionBadge').textContent = 'Нет связи с Raspberry Pi';
    $('#connectionBadge').className = 'connection offline';
  }
}

function formatDate(timestamp) { return new Date(timestamp * 1000).toLocaleString('ru-RU'); }

function renderMaps(maps, settings) {
  currentMaps = maps;
  selectedMap = selectedMap || settings?.default_map || maps[0]?.name || null;
  const list = $('#mapsList');
  if (!maps.length) {
    list.innerHTML = '<div class="notice">Сохранённых карт пока нет. Запустите картографирование, проедьте площадку и сохраните карту.</div>';
    return;
  }
  list.innerHTML = maps.map((map) => `
    <article class="map-item">
      ${map.image_exists ? `<img class="map-preview" src="/api/maps/${encodeURIComponent(map.name)}/preview.png?t=${map.modified_at}" alt="Карта ${map.name}">` : '<div class="map-preview empty">Нет изображения карты</div>'}
      <div class="map-body">
        <div class="map-title"><strong title="${map.name}">${map.name}</strong>${map.default ? '<span class="default-tag">Основная</span>' : ''}</div>
        <div class="map-meta">Изменена: ${formatDate(map.modified_at)}<br>Разрешение: ${map.resolution ?? '—'} м/пиксель</div>
        <div class="map-buttons">
          <button data-select-map="${map.name}" class="${selectedMap === map.name ? 'secondary' : ''}">Выбрать</button>
          <button data-default-map="${map.name}" ${map.default ? 'disabled' : ''}>Основная</button>
        </div>
      </div>
    </article>`).join('');

  $$('[data-select-map]').forEach((button) => button.addEventListener('click', () => {
    selectedMap = button.dataset.selectMap;
    renderMaps(currentMaps, lastSettings || settings);
    toast(`Выбрана карта: ${selectedMap}`, 'success');
  }));
  $$('[data-default-map]').forEach((button) => button.addEventListener('click', async () => {
    try {
      const result = await request('/api/maps/default', { method: 'POST', body: JSON.stringify({ name: button.dataset.defaultMap }) });
      lastSettings = result.settings;
      selectedMap = button.dataset.defaultMap;
      await refreshMaps();
      toast('Основная карта изменена', 'success');
    } catch (error) { toast(error.message, 'error'); }
  }));
}

async function refreshMaps() {
  try {
    const data = await request('/api/maps');
    lastSettings = data.settings;
    renderMaps(data.maps || [], data.settings || {});
  } catch (error) { toast(error.message, 'error'); }
}

async function sendDrive(action) {
  try { await request('/api/drive', { method: 'POST', body: JSON.stringify({ action }) }); }
  catch (error) { stopDrive(); toast(error.message, 'error'); }
}

function startDrive(button, action) {
  stopDrive(false);
  button.classList.add('active');
  sendDrive(action);
  driveTimer = setInterval(() => sendDrive(action), 180);
}

function stopDrive(send = true) {
  clearInterval(driveTimer);
  driveTimer = null;
  $$('.drive-button').forEach((button) => button.classList.remove('active'));
  if (send) sendDrive('stop');
}

$$('.drive-button').forEach((button) => {
  const action = button.dataset.drive;
  if (action === 'stop') { button.addEventListener('click', () => stopDrive(true)); return; }
  button.addEventListener('pointerdown', (event) => {
    event.preventDefault();
    button.setPointerCapture?.(event.pointerId);
    startDrive(button, action);
  });
  ['pointerup', 'pointercancel', 'lostpointercapture', 'pointerleave'].forEach((name) => button.addEventListener(name, () => stopDrive(true)));
});
window.addEventListener('blur', () => stopDrive(true));
document.addEventListener('visibilitychange', () => { if (document.hidden) stopDrive(true); });

$('#emergencyStop').addEventListener('click', async () => {
  stopDrive(true);
  try { await request('/api/mode/stop', { method: 'POST' }); toast('Движение и ROS-режим остановлены', 'success'); }
  catch (error) { toast(error.message, 'error'); }
});

$('#resetGpsAssist').addEventListener('click', async () => {
  try { const data = await request('/api/gps/reset-assist', { method: 'POST' }); toast(data.message || 'Привязка GPS сброшена', 'success'); }
  catch (error) { toast(error.message, 'error'); }
});

$('#startMapping').addEventListener('click', async () => {
  if (!confirm('Текущий ROS-режим будет остановлен. Начать построение новой карты?')) return;
  try { await request('/api/mode/mapping', { method: 'POST' }); toast('Картографирование запускается', 'success'); }
  catch (error) { toast(error.message, 'error'); }
});

$('#startNavigation').addEventListener('click', async () => {
  if (!selectedMap) return toast('Сначала выберите карту', 'error');
  try { await request('/api/mode/navigation', { method: 'POST', body: JSON.stringify({ map_name: selectedMap }) }); toast(`Запускается карта: ${selectedMap}`, 'success'); }
  catch (error) { toast(error.message, 'error'); }
});

$('#stopMode').addEventListener('click', async () => {
  try { await request('/api/mode/stop', { method: 'POST' }); toast('ROS-режим остановлен', 'success'); }
  catch (error) { toast(error.message, 'error'); }
});

const routeAction = async (operation, successMessage) => {
  try { const data = await request(`/api/route/${operation}`, { method: 'POST' }); toast(data.message || successMessage, 'success'); }
  catch (error) { toast(error.message, 'error'); }
};
$('#routeClear').addEventListener('click', () => routeAction('clear', 'Маршрут очищен'));
$('#routeStart').addEventListener('click', () => routeAction('start-recording', 'Запись начата'));
$('#routeStop').addEventListener('click', () => routeAction('stop-recording', 'Маршрут сохранён'));
$('#routePlay').addEventListener('click', () => routeAction('play', 'Маршрут запущен'));
$('#routeCancel').addEventListener('click', () => routeAction('cancel', 'Маршрут отменён'));

$('#saveMap').addEventListener('click', async () => {
  const name = $('#newMapName').value.trim();
  if (!name) return toast('Введите имя карты', 'error');
  try {
    await request('/api/maps/save', { method: 'POST', body: JSON.stringify({ name, set_default: $('#saveAsDefault').checked }) });
    $('#newMapName').value = '';
    await refreshMaps();
    toast('Карта сохранена', 'success');
  } catch (error) { toast(error.message, 'error'); }
});

$('#refreshMaps').addEventListener('click', refreshMaps);
$('#saveSettings').addEventListener('click', async () => {
  try {
    const data = await request('/api/settings', { method: 'POST', body: JSON.stringify({ auto_start: $('#autoStart').checked, startup_mode: $('#startupMode').value }) });
    lastSettings = data.settings;
    toast('Настройки автозапуска сохранены', 'success');
  } catch (error) { toast(error.message, 'error'); }
});

refreshMaps();
refreshStatus();
setInterval(refreshStatus, 1000);
setInterval(refreshMaps, 15000);
