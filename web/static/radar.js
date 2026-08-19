(() => {
  'use strict';

  const canvas = document.querySelector('#radarCanvas');
  const context = canvas.getContext('2d');
  const rangeSelect = document.querySelector('#rangeSelect');
  const emptyState = document.querySelector('#radarEmpty');
  const wsBadge = document.querySelector('#wsBadge');

  let socket = null;
  let reconnectTimer = null;
  let latestPayload = null;
  let previousScanTime = null;
  let measuredScanRate = null;

  const $ = (selector) => document.querySelector(selector);

  function websocketUrl(path) {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${protocol}//${window.location.host}${path}`;
  }

  function setText(selector, value) {
    const node = $(selector);
    if (node) node.textContent = value;
  }

  function finite(value, fallback = 0) {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  }

  function nullableNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function sensor(dotSelector, textSelector, state) {
    const dot = $(dotSelector);
    const text = $(textSelector);
    const online = Boolean(state?.online);
    if (dot) dot.className = `dot ${online ? 'online' : 'offline'}`;
    if (text) {
      text.textContent = online
        ? `${finite(state.age_sec).toFixed(2)} с назад`
        : 'нет данных';
    }
  }

  function gpsReason(reason) {
    if (!reason) return 'Ожидание данных';
    if (reason === 'accepted') return 'Координаты приняты';
    if (reason === 'no_fix') return 'Нет спутникового фикса';
    if (reason === 'no_coordinates') return 'Координаты ещё не определены';
    if (reason === 'no_hdop') return 'Нет оценки HDOP';
    if (reason.startsWith('few_satellites:')) return `Мало спутников: ${reason.split(':')[1]}`;
    if (reason.startsWith('hdop:')) return `Плохой HDOP: ${reason.split(':')[1]}`;
    if (reason.startsWith('jump:')) return `Отброшен скачок: ${reason.split(':')[1]}`;
    return reason;
  }

  function resizeCanvas() {
    const rect = canvas.getBoundingClientRect();
    const ratio = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
    const width = Math.max(320, Math.round(rect.width * ratio));
    const height = Math.max(320, Math.round(rect.height * ratio));
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
  }

  function drawGrid(maxRange) {
    const width = canvas.width;
    const height = canvas.height;
    const size = Math.min(width, height);
    const centerX = width / 2;
    const centerY = height / 2;
    const radius = size * 0.455;

    context.clearRect(0, 0, width, height);
    context.fillStyle = '#030c14';
    context.fillRect(0, 0, width, height);

    context.save();
    context.translate(centerX, centerY);

    const gradient = context.createRadialGradient(0, 0, 0, 0, 0, radius);
    gradient.addColorStop(0, 'rgba(14, 116, 144, 0.16)');
    gradient.addColorStop(1, 'rgba(2, 12, 20, 0.02)');
    context.fillStyle = gradient;
    context.beginPath();
    context.arc(0, 0, radius, 0, Math.PI * 2);
    context.fill();

    context.strokeStyle = 'rgba(56, 189, 248, 0.22)';
    context.lineWidth = Math.max(1, size / 800);
    context.fillStyle = 'rgba(143, 163, 184, 0.8)';
    context.font = `${Math.max(11, size / 65)}px system-ui`;

    for (let ring = 1; ring <= 4; ring += 1) {
      const ringRadius = radius * ring / 4;
      context.beginPath();
      context.arc(0, 0, ringRadius, 0, Math.PI * 2);
      context.stroke();
      const metres = maxRange * ring / 4;
      context.fillText(`${metres.toFixed(metres < 10 ? 1 : 0)} м`, 6, -ringRadius + 16);
    }

    for (let degree = 0; degree < 360; degree += 30) {
      const angle = degree * Math.PI / 180;
      context.beginPath();
      context.moveTo(0, 0);
      context.lineTo(-Math.sin(angle) * radius, -Math.cos(angle) * radius);
      context.stroke();
    }

    context.strokeStyle = 'rgba(229, 238, 248, 0.55)';
    context.beginPath();
    context.moveTo(-radius, 0);
    context.lineTo(radius, 0);
    context.moveTo(0, -radius);
    context.lineTo(0, radius);
    context.stroke();

    context.fillStyle = '#e5eef8';
    context.textAlign = 'center';
    context.fillText('0° / вперёд', 0, -radius - 8);
    context.fillText('180°', 0, radius + 20);
    context.textAlign = 'left';
    context.fillText('+90°', -radius + 6, -8);
    context.textAlign = 'right';
    context.fillText('-90°', radius - 6, -8);

    context.restore();
    return { centerX, centerY, radius };
  }

  function drawRobot(centerX, centerY, size) {
    context.save();
    context.translate(centerX, centerY);
    context.fillStyle = '#facc15';
    context.strokeStyle = '#fff7c2';
    context.lineWidth = Math.max(1, size / 100);
    context.beginPath();
    context.moveTo(0, -size * 0.05);
    context.lineTo(size * 0.028, size * 0.038);
    context.lineTo(0, size * 0.022);
    context.lineTo(-size * 0.028, size * 0.038);
    context.closePath();
    context.fill();
    context.stroke();
    context.restore();
  }

  function drawRadar(payload) {
    resizeCanvas();
    const maxRange = finite(rangeSelect.value, 6);
    const { centerX, centerY, radius } = drawGrid(maxRange);
    const points = Array.isArray(payload?.scan?.points) ? payload.scan.points : [];
    const scale = radius / maxRange;

    if (!points.length) {
      emptyState.classList.remove('hidden');
      drawRobot(centerX, centerY, Math.min(canvas.width, canvas.height));
      return;
    }

    emptyState.classList.add('hidden');
    context.save();
    context.translate(centerX, centerY);

    context.strokeStyle = 'rgba(53, 224, 143, 0.48)';
    context.fillStyle = '#35e08f';
    context.lineWidth = Math.max(1, Math.min(canvas.width, canvas.height) / 1100);
    context.beginPath();
    let pathStarted = false;

    for (const point of points) {
      const angle = finite(point[0]);
      const range = finite(point[1], NaN);
      if (!Number.isFinite(range) || range <= 0 || range > maxRange) continue;
      const x = -Math.sin(angle) * range * scale;
      const y = -Math.cos(angle) * range * scale;
      if (!pathStarted) {
        context.moveTo(x, y);
        pathStarted = true;
      } else {
        context.lineTo(x, y);
      }
    }
    if (pathStarted) context.stroke();

    const pointRadius = Math.max(1.4, Math.min(canvas.width, canvas.height) / 520);
    for (const point of points) {
      const angle = finite(point[0]);
      const range = finite(point[1], NaN);
      if (!Number.isFinite(range) || range <= 0 || range > maxRange) continue;
      const x = -Math.sin(angle) * range * scale;
      const y = -Math.cos(angle) * range * scale;
      context.beginPath();
      context.arc(x, y, pointRadius, 0, Math.PI * 2);
      context.fill();
    }
    context.restore();

    drawRobot(centerX, centerY, Math.min(canvas.width, canvas.height));
  }

  function updateHorizon(roll, pitch) {
    const horizon = $('#horizon');
    if (!horizon) return;
    const clampedPitch = Math.max(-45, Math.min(45, pitch));
    const offset = clampedPitch * 1.4;
    horizon.querySelectorAll('.horizon-sky, .horizon-ground, .horizon-line').forEach((element) => {
      element.style.transform = `translateY(${offset}px) rotate(${-roll}deg)`;
    });
  }

  function renderGps(gps) {
    const latitude = nullableNumber(gps?.latitude);
    const longitude = nullableNumber(gps?.longitude);
    const hdop = nullableNumber(gps?.hdop);
    const speed = nullableNumber(gps?.speed_mps);
    const course = nullableNumber(gps?.course_deg);
    const localX = nullableNumber(gps?.local_x);
    const localY = nullableNumber(gps?.local_y);
    const used = Number(gps?.satellites_used || 0);
    const visible = Number(gps?.satellites_visible || 0);

    setText('#gpsLatitude', latitude == null ? '—' : latitude.toFixed(7));
    setText('#gpsLongitude', longitude == null ? '—' : longitude.toFixed(7));
    setText('#gpsSatellites', visible > used ? `${used} / ${visible}` : String(used));
    setText('#gpsHdop', hdop == null ? '—' : hdop.toFixed(2));
    setText('#gpsSpeed', speed == null ? '—' : speed.toFixed(2));
    setText('#gpsCourse', course == null ? '—' : course.toFixed(1));
    setText('#gpsLocalX', localX == null ? '—' : localX.toFixed(2));
    setText('#gpsLocalY', localY == null ? '—' : localY.toFixed(2));
    setText('#gpsAssistState', gps?.assist_ready ? 'активна' : (gps?.alignment_state || 'ожидание'));
    setText('#gpsRejectReason', gpsReason(gps?.reject_reason));

    const badge = $('#gpsFixBadge');
    if (badge) {
      badge.textContent = gps?.fix_valid ? 'фикс есть' : 'нет фикса';
      badge.className = `gps-fix-badge ${gps?.fix_valid ? 'online' : 'offline'}`;
    }
  }

  function renderUltrasonic(ultrasonic, state) {
    const online = Boolean(state?.online);
    const valid = Boolean(ultrasonic?.valid) && online;
    const distance = nullableNumber(ultrasonic?.distance_cm);
    const stop = Boolean(ultrasonic?.stop);
    const emergency = Boolean(ultrasonic?.emergency);
    const near = Boolean(ultrasonic?.near);

    let zone = 'нет данных';
    if (valid) {
      if (emergency) zone = 'EMERGENCY';
      else if (stop) zone = 'STOP';
      else if (near) zone = 'NEAR';
      else zone = 'OK';
    }

    setText('#ultrasonicDistance', valid && distance != null ? distance.toFixed(1) : '—');
    setText('#ultrasonicZone', zone);
    setText('#ultrasonicStop', stop ? 'ДА' : 'нет');
    setText('#ultrasonicEmergency', emergency ? 'ДА' : 'нет');
  }

  function render(payload) {
    latestPayload = payload;
    drawRadar(payload);

    const ros = payload?.ros || {};
    const pose = ros.pose || {};
    const velocity = ros.velocity || {};
    const imu = payload?.imu || {};
    const gyro = imu.gyro || {};
    const tilt = imu.tilt || {};
    const scan = payload?.scan || {};
    const gps = payload?.gps || ros.gps || {};
    const ultrasonic = ros.ultrasonic || {};

    setText('#coordX', finite(pose.x).toFixed(3));
    setText('#coordY', finite(pose.y).toFixed(3));
    setText('#coordYaw', `${(finite(pose.yaw) * 180 / Math.PI).toFixed(1)}`);
    setText('#linearSpeed', finite(velocity.linear).toFixed(3));

    renderUltrasonic(ultrasonic, ros.sensors?.ultrasonic);
    renderGps(gps);

    setText('#gyroX', finite(gyro.x).toFixed(4));
    setText('#gyroY', finite(gyro.y).toFixed(4));
    setText('#gyroZ', finite(gyro.z).toFixed(4));
    setText('#rollValue', `${finite(tilt.roll_deg).toFixed(1)}°`);
    setText('#pitchValue', `${finite(tilt.pitch_deg).toFixed(1)}°`);
    setText('#imuAge', imu.age_sec == null ? '—' : `${finite(imu.age_sec).toFixed(2)} с`);
    updateHorizon(finite(tilt.roll_deg), finite(tilt.pitch_deg));

    const points = Array.isArray(scan.points) ? scan.points : [];
    const distances = points.map((point) => finite(point[1], NaN)).filter(Number.isFinite);
    setText('#pointCount', String(points.length));
    setText('#nearestRange', distances.length ? `${Math.min(...distances).toFixed(2)} м` : '—');
    setText('#scanAge', scan.age_sec == null ? '—' : `${finite(scan.age_sec).toFixed(2)} с`);

    if (scan.received_at) {
      const current = Number(scan.received_at);
      if (previousScanTime && current > previousScanTime) {
        const instant = 1 / (current - previousScanTime);
        measuredScanRate = measuredScanRate == null
          ? instant
          : measuredScanRate * 0.75 + instant * 0.25;
      }
      previousScanTime = current;
    }
    setText('#scanRate', measuredScanRate ? `${measuredScanRate.toFixed(1)} Гц` : '—');

    sensor('#lidarDot', '#lidarText', ros.sensors?.lidar);
    sensor('#imuDot', '#imuText', ros.sensors?.imu);
    sensor('#wheelDot', '#wheelText', ros.sensors?.wheel_odom);
    sensor('#odomDot', '#odomText', ros.sensors?.odom);
    sensor('#gpsDot', '#gpsText', ros.sensors?.gps);
    sensor('#ultrasonicDot', '#ultrasonicText', ros.sensors?.ultrasonic);
  }

  function scheduleReconnect() {
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connect, 1800);
  }

  function connect() {
    clearTimeout(reconnectTimer);
    if (socket && socket.readyState <= WebSocket.OPEN) return;

    wsBadge.textContent = 'Подключение…';
    wsBadge.className = 'connection offline';
    socket = new WebSocket(websocketUrl('/ws/radar'));

    socket.addEventListener('open', () => {
      wsBadge.textContent = 'WebSocket подключён';
      wsBadge.className = 'connection online';
    });

    socket.addEventListener('message', (event) => {
      try {
        render(JSON.parse(event.data));
      } catch (error) {
        console.error('RobotLidar radar JSON error:', error);
      }
    });

    socket.addEventListener('close', () => {
      wsBadge.textContent = 'WebSocket отключён';
      wsBadge.className = 'connection offline';
      scheduleReconnect();
    });

    socket.addEventListener('error', () => socket?.close());
  }

  rangeSelect.addEventListener('change', () => {
    if (latestPayload) drawRadar(latestPayload);
  });
  window.addEventListener('resize', () => {
    if (latestPayload) drawRadar(latestPayload);
  });
  window.addEventListener('online', connect);

  resizeCanvas();
  drawRadar(null);
  connect();
})();
