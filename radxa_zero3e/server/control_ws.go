package main

import (
	"fmt"
	"log"
	"net/http"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gorilla/websocket"
)

type controlChannel struct {
	conn   *websocket.Conn
	writeM sync.Mutex
	lastMS atomic.Int64
	closed atomic.Bool
}

var controlWSUpgrader = websocket.Upgrader{
	ReadBufferSize:  1024,
	WriteBufferSize: 1024,
	CheckOrigin: func(_ *http.Request) bool { return true },
}

func (c *controlChannel) send(packet []byte) error {
	if c == nil || c.conn == nil || c.closed.Load() {
		return fmt.Errorf("control websocket is not connected")
	}
	c.writeM.Lock()
	defer c.writeM.Unlock()
	_ = c.conn.SetWriteDeadline(time.Now().Add(2 * time.Second))
	if err := c.conn.WriteMessage(websocket.BinaryMessage, packet); err != nil {
		return err
	}
	c.lastMS.Store(time.Now().UnixMilli())
	return nil
}

func (c *controlChannel) pong(appData string) error {
	if c == nil || c.conn == nil || c.closed.Load() {
		return nil
	}
	c.writeM.Lock()
	defer c.writeM.Unlock()
	return c.conn.WriteControl(websocket.PongMessage, []byte(appData), time.Now().Add(2*time.Second))
}

func (c *controlChannel) close() {
	if c == nil || c.conn == nil || c.closed.Swap(true) {
		return
	}
	_ = c.conn.Close()
}

func (s *server) controlWebSocket(w http.ResponseWriter, r *http.Request, id string) {
	if r.Method != http.MethodGet {
		methodNotAllowed(w)
		return
	}

	s.devicesM.RLock()
	d := s.devices[id]
	s.devicesM.RUnlock()
	if d == nil {
		writeError(w, http.StatusNotFound, "Device not registered")
		return
	}

	conn, err := controlWSUpgrader.Upgrade(w, r, nil)
	if err != nil {
		log.Printf("CONTROL/WSS %s upgrade error: %v", id, err)
		return
	}
	ch := &controlChannel{conn: conn}
	ch.lastMS.Store(time.Now().UnixMilli())

	d.controlM.Lock()
	old := d.controlWS
	d.controlWS = ch
	d.controlM.Unlock()
	if old != nil {
		old.close()
	}

	log.Printf("CONTROL/WSS %s connected from %s", id, r.RemoteAddr)
	defer func() {
		ch.close()
		d.controlM.Lock()
		if d.controlWS == ch {
			d.controlWS = nil
		}
		d.controlM.Unlock()
		log.Printf("CONTROL/WSS %s disconnected", id)
	}()

	conn.SetReadLimit(4096)
	_ = conn.SetReadDeadline(time.Now().Add(30 * time.Second))
	conn.SetPingHandler(func(appData string) error {
		ch.lastMS.Store(time.Now().UnixMilli())
		if err := conn.SetReadDeadline(time.Now().Add(30 * time.Second)); err != nil {
			return err
		}
		return ch.pong(appData)
	})
	conn.SetPongHandler(func(string) error {
		ch.lastMS.Store(time.Now().UnixMilli())
		return conn.SetReadDeadline(time.Now().Add(30 * time.Second))
	})

	for {
		messageType, _, err := conn.ReadMessage()
		if err != nil {
			return
		}
		ch.lastMS.Store(time.Now().UnixMilli())
		if messageType == websocket.CloseMessage {
			return
		}
	}
}

func (d *device) websocketControlConnected() bool {
	d.controlM.RLock()
	ch := d.controlWS
	d.controlM.RUnlock()
	return ch != nil && !ch.closed.Load()
}

func (d *device) sendControlWebSocket(packet []byte) error {
	d.controlM.RLock()
	ch := d.controlWS
	d.controlM.RUnlock()
	if ch == nil {
		return fmt.Errorf("control websocket is not connected")
	}
	return ch.send(packet)
}
