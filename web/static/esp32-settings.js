(() => {
  'use strict';

  const ids = [
    'us_enabled','us_warn_mm','us_stop_mm','us_emergency_mm','us_clear_mm',
    'us_danger_samples','us_clear_samples','us_sample_ms','hall_enabled',
    'hall_left_inverted','hall_right_inverted','hall_ppr','wheel_circ_mm','track_width_mm'
  ];

  const $ = (id) => document.getElementById(id);
  const statusBadge = $('statusBadge');
  const message = $('message');
  let loaded = false;

  function showMessage(text, kind='') {
    message.textContent = text;
    message.className = `message ${kind}`;
  }

  function setOnline(online) {
    statusBadge.textContent = online ? 'ESP32 подключена' : 'ESP32 нет данных';
    statusBadge.className = `badge ${online ? 'online' : 'offline'}`;
  }

  function applyConfig(config) {
    if (!config || !config.version) return;
    for (const id of ids) {
      if (!(id in config)) continue;
      const el = $(id);
      if (!el) continue;
      if (el.type === 'checkbox') el.checked = Boolean(config[id]);
      else el.value = config[id];
    }
    $('configVersion').textContent = String(config.version ?? '—');
    $('lastUpdate').textContent = config.received_at
      ? `Последний ответ: ${new Date(config.received_at * 1000).toLocaleTimeString()}`
      : 'Ответ получен';
    setOnline(Boolean(config.online));
    loaded = true;
  }

  async function loadConfig(show=true) {
    try {
      const response = await fetch('/api/esp32/config', {cache:'no-store'});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Ошибка чтения настроек');
      applyConfig(data.config);
      if (show && data.config?.online) showMessage('Настройки считаны с ESP32.', 'ok');
    } catch (error) {
      setOnline(false);
      if (show) showMessage(error.message, 'error');
    }
  }

  function collectValues() {
    const values = {};
    for (const id of ids) {
      const el = $(id);
      if (el.type === 'checkbox') values[id] = el.checked;
      else {
        const value = Number(el.value);
        if (!Number.isFinite(value)) throw new Error(`Неверное значение: ${id}`);
        values[id] = Math.round(value);
      }
    }
    if (values.us_emergency_mm > values.us_stop_mm) {
      throw new Error('Аварийная дистанция должна быть не больше дистанции STOP.');
    }
    if (values.us_warn_mm < values.us_stop_mm) {
      throw new Error('Предупреждение должно быть не ближе, чем STOP.');
    }
    return values;
  }

  async function saveConfig() {
    try {
      const values = collectValues();
      const response = await fetch('/api/esp32/config', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({values}),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Ошибка сохранения');
      showMessage('Команда сохранения отправлена. ESP32 записывает параметры в NVS…', 'ok');
      setTimeout(() => loadConfig(false), 500);
      setTimeout(() => loadConfig(false), 1200);
    } catch (error) {
      showMessage(error.message, 'error');
    }
  }

  async function resetConfig() {
    if (!confirm('Сбросить настройки ESP32 к значениям по умолчанию?')) return;
    try {
      const response = await fetch('/api/esp32/config/reset', {method:'POST'});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Ошибка сброса');
      showMessage('Сброс отправлен на ESP32.', 'ok');
      setTimeout(() => loadConfig(false), 700);
    } catch (error) {
      showMessage(error.message, 'error');
    }
  }

  $('reloadButton').addEventListener('click', () => loadConfig(true));
  $('saveButton').addEventListener('click', saveConfig);
  $('resetButton').addEventListener('click', resetConfig);

  loadConfig(false);
  setInterval(() => loadConfig(false), 3000);
  setTimeout(() => { if (!loaded) showMessage('Ожидание ответа от ESP32…'); }, 1200);
})();
