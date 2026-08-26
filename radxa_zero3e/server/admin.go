package main

import (
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"
)

const adminSessionTTL = 12 * time.Hour

type adminAuthRequest struct {
	Username string `json:"username"`
	Password string `json:"password"`
}

type adminCreateUserRequest struct {
	Username string `json:"username"`
	Password string `json:"password"`
}

type adminPasswordRequest struct {
	Password string `json:"password"`
}

func (s *server) adminLogin(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		methodNotAllowed(w)
		return
	}
	adminPassword := os.Getenv("ADMIN_PASSWORD")
	if strings.TrimSpace(adminPassword) == "" {
		writeError(w, http.StatusServiceUnavailable, "ADMIN_PASSWORD is not configured")
		return
	}
	adminUsername := env("ADMIN_USERNAME", "admin")
	var req adminAuthRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	if !constantStringEqual(strings.TrimSpace(req.Username), adminUsername) || !constantStringEqual(req.Password, adminPassword) {
		writeError(w, http.StatusUnauthorized, "Неверный логин или пароль администратора")
		return
	}
	token, err := newRandomToken()
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	s.adminSessionM.Lock()
	s.adminSessions[token] = time.Now().Add(adminSessionTTL)
	s.adminSessionM.Unlock()
	writeJSON(w, http.StatusOK, map[string]any{
		"token": token,
		"admin": map[string]any{"username": adminUsername},
	})
}

func (s *server) adminLogout(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		methodNotAllowed(w)
		return
	}
	if token := bearerToken(r); token != "" {
		s.adminSessionM.Lock()
		delete(s.adminSessions, token)
		s.adminSessionM.Unlock()
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (s *server) adminMe(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		methodNotAllowed(w)
		return
	}
	if !s.requireAdmin(w, r) {
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"username": env("ADMIN_USERNAME", "admin")})
}

func (s *server) adminUsers(w http.ResponseWriter, r *http.Request) {
	if !s.requireAdmin(w, r) {
		return
	}
	switch r.Method {
	case http.MethodGet:
		rows, err := s.db.Query(`
SELECT u.id,u.username,u.created_at,COUNT(ud.device_id)
FROM users u
LEFT JOIN user_devices ud ON ud.user_id=u.id
GROUP BY u.id,u.username,u.created_at
ORDER BY u.id`)
		if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		defer rows.Close()
		users := []map[string]any{}
		for rows.Next() {
			var id, createdAt, deviceCount int64
			var username string
			if err := rows.Scan(&id, &username, &createdAt, &deviceCount); err != nil {
				continue
			}
			users = append(users, map[string]any{
				"id": id, "username": username, "created_at": createdAt, "device_count": deviceCount,
			})
		}
		writeJSON(w, http.StatusOK, map[string]any{"users": users})
	case http.MethodPost:
		var req adminCreateUserRequest
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
				writeError(w, http.StatusConflict, "Пользователь с таким логином уже существует")
			} else {
				writeError(w, http.StatusInternalServerError, err.Error())
			}
			return
		}
		id, _ := res.LastInsertId()
		writeJSON(w, http.StatusCreated, map[string]any{"ok": true, "user": map[string]any{"id": id, "username": req.Username}})
	default:
		methodNotAllowed(w)
	}
}

func (s *server) adminUserByID(w http.ResponseWriter, r *http.Request) {
	if !s.requireAdmin(w, r) {
		return
	}
	rest := strings.Trim(strings.TrimPrefix(r.URL.Path, "/api/admin/users/"), "/")
	parts := strings.Split(rest, "/")
	if len(parts) == 0 || parts[0] == "" {
		writeError(w, http.StatusBadRequest, "User ID required")
		return
	}
	id, err := strconv.ParseInt(parts[0], 10, 64)
	if err != nil || id <= 0 {
		writeError(w, http.StatusBadRequest, "Invalid user ID")
		return
	}

	if len(parts) == 1 && r.Method == http.MethodDelete {
		res, err := s.db.Exec(`DELETE FROM users WHERE id=?`, id)
		if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		n, _ := res.RowsAffected()
		if n == 0 {
			writeError(w, http.StatusNotFound, "User not found")
			return
		}
		s.invalidateUserSessions(id)
		writeJSON(w, http.StatusOK, map[string]any{"ok": true})
		return
	}

	if len(parts) == 2 && parts[1] == "password" && r.Method == http.MethodPost {
		var req adminPasswordRequest
		if !decodeJSON(w, r, &req) {
			return
		}
		if len(req.Password) < 4 || len(req.Password) > 128 {
			writeError(w, http.StatusBadRequest, "Пароль: 4–128 символов")
			return
		}
		hash, err := hashPassword(req.Password)
		if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		res, err := s.db.Exec(`UPDATE users SET password_hash=? WHERE id=?`, hash, id)
		if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		n, _ := res.RowsAffected()
		if n == 0 {
			writeError(w, http.StatusNotFound, "User not found")
			return
		}
		s.invalidateUserSessions(id)
		writeJSON(w, http.StatusOK, map[string]any{"ok": true})
		return
	}

	methodNotAllowed(w)
}

func (s *server) requireAdmin(w http.ResponseWriter, r *http.Request) bool {
	token := bearerToken(r)
	if token == "" {
		writeError(w, http.StatusUnauthorized, "Admin authentication required")
		return false
	}
	now := time.Now()
	s.adminSessionM.RLock()
	expires, ok := s.adminSessions[token]
	s.adminSessionM.RUnlock()
	if !ok || now.After(expires) {
		if ok {
			s.adminSessionM.Lock()
			delete(s.adminSessions, token)
			s.adminSessionM.Unlock()
		}
		writeError(w, http.StatusUnauthorized, "Admin session expired")
		return false
	}
	return true
}

func (s *server) invalidateUserSessions(userID int64) {
	s.sessionM.Lock()
	for token, id := range s.sessions {
		if id == userID {
			delete(s.sessions, token)
		}
	}
	s.sessionM.Unlock()
}

func newRandomToken() (string, error) {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(b), nil
}

func constantStringEqual(a, b string) bool {
	ha := sha256.Sum256([]byte(a))
	hb := sha256.Sum256([]byte(b))
	return subtle.ConstantTimeCompare(ha[:], hb[:]) == 1
}
