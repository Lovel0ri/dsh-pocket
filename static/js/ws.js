/* dsh-pocket · WebSocket client (personal LAN mode, no ticket/auth).
   Simple exponential backoff + heartbeat + resubscribe. Contract: docs/api.md §4. */
(function () {
  'use strict';

  var DSH = window.DSH;
  var WS_PATH = '/api/v1/ws';
  var HEARTBEAT_MS = 15000;
  var PONG_TIMEOUT_MS = 10000;

  function WsClient(options) {
    this.opts = options || {};
    this.socket = null;
    this.status = 'offline';
    this.attempt = 0;
    this.stopped = true;
    this.sessionId = null;
    this._connecting = false;
    this._reconnectTimer = null;
    this._heartbeatTimer = null;
    this._pongTimer = null;
    this._nonce = 0;
  }

  WsClient.prototype._emit = function (name) {
    var args = Array.prototype.slice.call(arguments, 1);
    var fn = this.opts[name];
    if (typeof fn !== 'function') return;
    try { fn.apply(null, args); } catch (err) {
      if (window.console && console.error) console.error('ws handler error', err);
    }
  };

  WsClient.prototype._setStatus = function (status) {
    if (this.status === status) return;
    this.status = status;
    this._emit('onStatus', status);
  };

  WsClient.prototype.isConnected = function () {
    return this.status === 'connected' && !!this.socket && this.socket.readyState === 1;
  };

  WsClient.prototype.start = function () {
    this.stopped = false;
    if (this.isConnected() || this._connecting) return;
    this.attempt = 0;
    this.connect();
  };

  WsClient.prototype.connect = function () {
    if (this.stopped || this._connecting || this.isConnected()) return;

    var self = this;
    var proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    var url = proto + '//' + window.location.host + WS_PATH;

    this._connecting = true;
    this._setStatus(this.attempt > 0 ? 'reconnecting' : 'connecting');

    var socket;
    try {
      socket = new WebSocket(url);
    } catch (err) {
      this._connecting = false;
      this._scheduleReconnect();
      return;
    }
    this.socket = socket;

    socket.onopen = function () {
      if (self.stopped) {
        try { socket.close(1000, 'client stopped'); } catch (err) { /* noop */ }
        return;
      }
      self._connecting = false;
      self.attempt = 0;
      self._setStatus('connected');
      self._startHeartbeat();
      self._emit('onOpen');
      if (self.sessionId) self.send({ type: 'subscribe', sessionId: self.sessionId });
    };

    socket.onmessage = function (event) {
      var message;
      try { message = JSON.parse(String(event.data || '')); } catch (err) { return; }
      if (!message || typeof message.type !== 'string') return;
      if (message.type === 'pong') {
        if (self._pongTimer) {
          clearTimeout(self._pongTimer);
          self._pongTimer = null;
        }
        return;
      }
      self._emit('onEvent', message);
    };

    socket.onerror = function () { /* close handler schedules the retry */ };

    socket.onclose = function () {
      self._connecting = false;
      if (self.socket === socket) self.socket = null;
      self._stopHeartbeat();
      if (self.stopped) {
        self._setStatus('offline');
        return;
      }
      self._setStatus('reconnecting');
      self._scheduleReconnect();
    };
  };

  WsClient.prototype._scheduleReconnect = function () {
    if (this.stopped || this._reconnectTimer) return;
    var self = this;
    var delay = Math.min(15000, 600 * Math.pow(1.8, this.attempt)) +
      Math.floor(Math.random() * 300);
    this.attempt += 1;
    this._setStatus('reconnecting');
    this._reconnectTimer = setTimeout(function () {
      self._reconnectTimer = null;
      self.connect();
    }, delay);
  };

  WsClient.prototype._startHeartbeat = function () {
    var self = this;
    this._stopHeartbeat();
    this._heartbeatTimer = setInterval(function () {
      if (!self.isConnected()) return;
      self._nonce += 1;
      self.send({ type: 'ping', nonce: self._nonce });
      if (self._pongTimer) clearTimeout(self._pongTimer);
      self._pongTimer = setTimeout(function () {
        self._pongTimer = null;
        if (self.socket) {
          try { self.socket.close(4001, 'pong timeout'); } catch (err) { /* noop */ }
        }
      }, PONG_TIMEOUT_MS);
    }, HEARTBEAT_MS);
  };

  WsClient.prototype._stopHeartbeat = function () {
    if (this._heartbeatTimer) {
      clearInterval(this._heartbeatTimer);
      this._heartbeatTimer = null;
    }
    if (this._pongTimer) {
      clearTimeout(this._pongTimer);
      this._pongTimer = null;
    }
  };

  WsClient.prototype.send = function (payload) {
    if (!this.isConnected()) return false;
    try {
      this.socket.send(JSON.stringify(payload));
      return true;
    } catch (err) {
      return false;
    }
  };

  WsClient.prototype.subscribeSession = function (sessionId) {
    this.sessionId = sessionId || null;
    if (this.isConnected() && this.sessionId) {
      this.send({ type: 'subscribe', sessionId: this.sessionId });
    }
  };

  WsClient.prototype.leaveSession = function () {
    if (this.sessionId && this.isConnected()) {
      this.send({ type: 'unsubscribe', sessionId: this.sessionId });
    }
    this.sessionId = null;
  };

  WsClient.prototype.kick = function () {
    if (this.stopped) { this.start(); return; }
    if (this.isConnected() || this._connecting) return;
    if (this._reconnectTimer) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
    this.connect();
  };

  WsClient.prototype.close = function () {
    this.stopped = true;
    this._stopHeartbeat();
    if (this._reconnectTimer) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
    if (this.socket) {
      try { this.socket.close(1000, 'client close'); } catch (err) { /* noop */ }
      this.socket = null;
    }
    this._connecting = false;
    this._setStatus('offline');
  };

  DSH.WsClient = WsClient;
})();
