package main

import (
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"database/sql"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"

	"golang.org/x/crypto/pbkdf2"
)

type authRequest struct {
	Username string `json:"username"`
	Password string `json:"password"`
}

type attachRequest struct {
	DeviceID string  `json:"device_id"`
	Alias    *string `json:"alias"`
}

func initDB(db *sql.DB) error {
	_, err := db.Exec(`
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS user_devices (
    user_id INTEGER NOT NULL,
    device_id TEXT NOT NULL,
    alias TEXT,
    created_at INTEGER NOT NULL,
    PRIMARY KEY(user_id, device_id),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_device_one_owner ON user_devices(device_id);
`)
	return err
}

func (s *server) authRegister(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		methodNotAllowed(w)
		return
	}
	var req authRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	req.Username = strings.TrimSpace(req.Username)
	if len(req.Username) < 3 || len(req.Username) > 64 || len(req.Password) < 4 || len(req.Password) > 128 {
		writeError(w, http.StatusBadRequest, "Логин: 3–64 символа, пароль: 4–128 символов")
		return
	}

	hash, err := hashPassword(req.Password)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	res, err := s.db.Exec(`INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)`, req.Username, hash, time.Now().Unix())
	if err != nil {
		if strings.Contains(strings.ToLower(err.Error()), "unique") {
			writeError(w, http.StatusConflict, "Username already exists")
		} else {
			writeError(w, http.StatusInternalServerError, err.Error())
		}
		return
	}
	id, _ := res.LastInsertId()
	token, err := s.newSession(id)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"token": token,
		"user":  map[string]any{"id": id, "username": req.Username},
	})
}

func (s *server) authLogin(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		methodNotAllowed(w)
		return
	}
	var req authRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	req.Username = strings.TrimSpace(req.Username)

	var id int64
	var username, encoded string
	err := s.db.QueryRow(`SELECT id,username,password_hash FROM users WHERE username=? COLLATE NOCASE`, req.Username).Scan(&id, &username, &encoded)
	if err != nil || !verifyPassword(req.Password, encoded) {
		writeError(w, http.StatusUnauthorized, "Invalid login or password")
		return
	}
	token, err := s.newSession(id)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"token": token,
		"user":  map[string]any{"id": id, "username": username},
	})
}

func (s *server) authMe(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		methodNotAllowed(w)
		return
	}
	u, ok := s.requireUser(w, r)
	if !ok {
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"id": u.ID, "username": u.Username})
}

func (s *server) authLogout(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		methodNotAllowed(w)
		return
	}
	if token := bearerToken(r); token != "" {
		s.sessionM.Lock()
		delete(s.sessions, token)
		s.sessionM.Unlock()
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (s *server) settingsDevices(w http.ResponseWriter, r *http.Request) {
	u, ok := s.requireUser(w, r)
	if !ok {
		return
	}

	switch r.Method {
	case http.MethodGet:
		rows, err := s.db.Query(`SELECT device_id,alias,created_at FROM user_devices WHERE user_id=? ORDER BY created_at`, u.ID)
		if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		defer rows.Close()
		out := []map[string]any{}
		for rows.Next() {
			var id string
			var alias sql.NullString
			var created int64
			if err := rows.Scan(&id, &alias, &created); err != nil {
				continue
			}
			var a any
			if alias.Valid {
				a = alias.String
			}
			out = append(out, map[string]any{"device_id": id, "alias": a, "created_at": created})
		}
		writeJSON(w, http.StatusOK, map[string]any{"devices": out})

	case http.MethodPost:
		var req attachRequest
		if !decodeJSON(w, r, &req) {
			return
		}
		req.DeviceID = strings.TrimSpace(req.DeviceID)
		if len(req.DeviceID) < 3 || len(req.DeviceID) > 128 {
			writeError(w, http.StatusBadRequest, "Device ID is required")
			return
		}

		var owner int64
		err := s.db.QueryRow(`SELECT user_id FROM user_devices WHERE device_id=?`, req.DeviceID).Scan(&owner)
		if err == nil && owner != u.ID {
			writeError(w, http.StatusConflict, "This tractor ID is already linked to another account")
			return
		}
		var alias any
		if req.Alias != nil && strings.TrimSpace(*req.Alias) != "" {
			alias = strings.TrimSpace(*req.Alias)
		}
		_, err = s.db.Exec(`INSERT INTO user_devices(user_id,device_id,alias,created_at) VALUES(?,?,?,?) ON CONFLICT(user_id,device_id) DO UPDATE SET alias=excluded.alias`, u.ID, req.DeviceID, alias, time.Now().Unix())
		if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"ok": true, "device_id": req.DeviceID})

	default:
		methodNotAllowed(w)
	}
}

func (s *server) settingsDeviceByID(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodDelete {
		methodNotAllowed(w)
		return
	}
	u, ok := s.requireUser(w, r)
	if !ok {
		return
	}
	id := strings.Trim(strings.TrimPrefix(r.URL.Path, "/api/settings/devices/"), "/")
	if id == "" {
		writeError(w, http.StatusBadRequest, "Device ID required")
		return
	}
	if _, err := s.db.Exec(`DELETE FROM user_devices WHERE user_id=? AND device_id=?`, u.ID, id); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (s *server) requireUser(w http.ResponseWriter, r *http.Request) (user, bool) {
	token := bearerToken(r)
	if token == "" {
		writeError(w, http.StatusUnauthorized, "Authentication required")
		return user{}, false
	}
	s.sessionM.RLock()
	id, ok := s.sessions[token]
	s.sessionM.RUnlock()
	if !ok {
		writeError(w, http.StatusUnauthorized, "Session expired")
		return user{}, false
	}
	var u user
	if err := s.db.QueryRow(`SELECT id,username FROM users WHERE id=?`, id).Scan(&u.ID, &u.Username); err != nil {
		writeError(w, http.StatusUnauthorized, "User not found")
		return user{}, false
	}
	return u, true
}

func (s *server) newSession(userID int64) (string, error) {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	token := base64.RawURLEncoding.EncodeToString(b)
	s.sessionM.Lock()
	s.sessions[token] = userID
	s.sessionM.Unlock()
	return token, nil
}

func hashPassword(password string) (string, error) {
	salt := make([]byte, 16)
	if _, err := rand.Read(salt); err != nil {
		return "", err
	}
	const rounds = 200000
	digest := pbkdf2.Key([]byte(password), salt, rounds, 32, sha256.New)
	return fmt.Sprintf("pbkdf2_sha256$%d$%s$%s", rounds, hex.EncodeToString(salt), hex.EncodeToString(digest)), nil
}

func verifyPassword(password, encoded string) bool {
	parts := strings.Split(encoded, "$")
	if len(parts) != 4 || parts[0] != "pbkdf2_sha256" {
		return false
	}
	rounds, err := strconv.Atoi(parts[1])
	if err != nil {
		return false
	}
	salt, err := hex.DecodeString(parts[2])
	if err != nil {
		return false
	}
	want, err := hex.DecodeString(parts[3])
	if err != nil {
		return false
	}
	got := pbkdf2.Key([]byte(password), salt, rounds, len(want), sha256.New)
	return subtle.ConstantTimeCompare(got, want) == 1
}
