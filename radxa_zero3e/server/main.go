package main

import (
	"database/sql"
	"embed"
	"errors"
	"io/fs"
	"log"
	"net"
	"net/http"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	_ "modernc.org/sqlite"
)

const (
	offlineAfter   = 5 * time.Second
	videoAfter     = 3 * time.Second
	videoPortBase  = 10000
	ptzMagic       = 0x5354
	ptzVersion     = 1
	ptzType        = 1
	flagCenter     = 1 << 0
	flagRequestIDR = 1 << 1
)

//go:embed web/*
var webFiles embed.FS

type server struct {
	db       *sql.DB
	devices  map[string]*device
	devicesM sync.RWMutex
	sessions map[string]int64
	sessionM sync.RWMutex
	seq      atomic.Uint32
	ptzConn  *net.UDPConn
}

type device struct {
	ID         string
	Name       string
	DeviceType string
	IP         string
	PTZPort    int
	RTPPort    int
	SRTPort    int
	Transport  string
	LastSeen   atomic.Int64
	FPS        atomic.Int64
	Bitrate    atomic.Int64
	Dropped    atomic.Int64
	UptimeMS   atomic.Int64
	PanCDeg    atomic.Int64
	TiltCDeg   atomic.Int64
	LinkMbps   atomic.Int64
	stream     *rtpStream
	srt        *srtBridge
}

type user struct {
	ID       int64
	Username string
}

func main() {
	addr := env("LISTEN_ADDR", "0.0.0.0:8000")
	dbPath := env("DB_PATH", "camera_hub.db")

	dbh, err := sql.Open("sqlite", dbPath)
	if err != nil {
		log.Fatal(err)
	}
	defer dbh.Close()
	if _, err = dbh.Exec(`PRAGMA foreign_keys=ON; PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;`); err != nil {
		log.Fatal(err)
	}
	if err = initDB(dbh); err != nil {
		log.Fatal(err)
	}

	ptzConn, err := net.ListenUDP("udp", nil)
	if err != nil {
		log.Fatal(err)
	}
	defer ptzConn.Close()

	s := &server{
		db:       dbh,
		devices:  make(map[string]*device),
		sessions: make(map[string]int64),
		ptzConn:  ptzConn,
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/api/auth/register", s.authRegister)
	mux.HandleFunc("/api/auth/login", s.authLogin)
	mux.HandleFunc("/api/auth/me", s.authMe)
	mux.HandleFunc("/api/auth/logout", s.authLogout)
	mux.HandleFunc("/api/settings/devices", s.settingsDevices)
	mux.HandleFunc("/api/settings/devices/", s.settingsDeviceByID)
	mux.HandleFunc("/api/devices", s.listDevices)
	mux.HandleFunc("/api/devices/", s.deviceAPI)
	mux.HandleFunc("/health", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"ok": true})
	})

	webRoot, err := fs.Sub(webFiles, "web")
	if err != nil {
		log.Fatal(err)
	}
	static := http.FileServer(http.FS(webRoot))
	mux.Handle("/static/", http.StripPrefix("/static/", static))
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" {
			http.NotFound(w, r)
			return
		}
		data, err := fs.ReadFile(webRoot, "index.html")
		if err != nil {
			http.Error(w, "index unavailable", http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = w.Write(data)
	})

	srv := &http.Server{
		Addr:              addr,
		Handler:           cors(logging(mux)),
		ReadHeaderTimeout: 5 * time.Second,
	}

	log.Printf("RobotLiDAR Go server listening on http://%s", addr)
	log.Printf("SQLite: %s", dbPath)
	log.Printf("H.264: direct RTP -> Pion WebRTC, no decode/encode")
	log.Printf("Reliable uplink: pure-Go SRT/MPEG-TS UDP %d-%d -> H.264 RTP -> Pion", srtPortBase, srtPortMax)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal(err)
	}
}

func env(key, def string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return def
}
