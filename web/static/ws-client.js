(() => {
  'use strict';

  // app.js historically polled /api/status once per second. Keep its initial
  // HTTP request as a startup fallback, but suppress the recurring telemetry
  // poll because live state now arrives through WebSocket.
  const nativeSetInterval = window.setInterval.bind(window);
  window.setInterval = (callback, delay, ...args) => {
    if (
      delay === 1000
      && typeof callback === 'function'
      && callback.name === 'refreshStatus'
    ) {
      return -1;
    }
    return nativeSetInterval(callback, delay, ...args);
  };

  let socket = null;
  let reconnectTimer = null;

  function websocketUrl(path) {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${protocol}//${window.location.host}${path}`;
  }

  function showConnection(text, online) {
    const badge = document.querySelector('#connectionBadge');
    if (!badge) return;
    badge.textContent = text;
    badge.className = `connection ${online ? 'online' : 'offline'}`;
  }

  function scheduleReconnect() {
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connect, 2000);
  }

  function connect() {
    clearTimeout(reconnectTimer);
    if (socket && socket.readyState <= WebSocket.OPEN) return;

    showConnection('Подключение WebSocket…', false);
    socket = new WebSocket(websocketUrl('/ws/status'));

    socket.addEventListener('open', () => {
      showConnection('Raspberry Pi подключена • WebSocket', true);
    });

    socket.addEventListener('message', (event) => {
      try {
        const payload = JSON.parse(event.data);
        if (typeof window.renderStatus === 'function') {
          window.renderStatus(payload);
        }
        showConnection('Raspberry Pi подключена • WebSocket', true);
      } catch (error) {
        console.error('RobotLidar WebSocket JSON error:', error);
      }
    });

    socket.addEventListener('close', () => {
      showConnection('Нет связи WebSocket', false);
      scheduleReconnect();
    });

    socket.addEventListener('error', () => {
      socket?.close();
    });
  }

  window.addEventListener('DOMContentLoaded', connect);
  window.addEventListener('online', connect);
})();
