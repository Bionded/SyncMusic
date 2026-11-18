// SPDX-License-Identifier: GPL-3.0-or-later
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func updateServer(t *testing.T, files map[string]string, damage string) *httptest.Server {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/client/v1/app-update", func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer tok" || r.URL.Query().Get("app") != appName {
			http.Error(w, "no", 401)
			return
		}
		if r.URL.Query().Get("version") == "9.0.0" {
			_ = json.NewEncoder(w).Encode(map[string]any{"available": false})
			return
		}
		var fs []appFile
		for name, body := range files {
			h := sha256.Sum256([]byte(body))
			fs = append(fs, appFile{name, int64(len(body)), hex.EncodeToString(h[:]), "app-update/" + appName + "/9.0.0/" + name})
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"available": true, "version": "9.0.0", "files": fs})
	})
	mux.HandleFunc("/api/client/v1/app-update/"+appName+"/9.0.0/", func(w http.ResponseWriter, r *http.Request) {
		name := strings.TrimPrefix(r.URL.Path, "/api/client/v1/app-update/"+appName+"/9.0.0/")
		body := files[name]
		if name == damage {
			body += "!"
		}
		_, _ = w.Write([]byte(body))
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv
}

func updateClient(t *testing.T, srv string) *Client {
	root := t.TempDir()
	dir := filepath.Join(root, ".apps", "syncmusic")
	_ = os.MkdirAll(dir, 0o755)
	_ = os.WriteFile(filepath.Join(dir, appName), []byte("old binary"), 0o755)
	_ = os.WriteFile(filepath.Join(dir, "icon.png"), []byte("old icon"), 0o644)
	data := filepath.Join(root, ".apps-data", "syncmusic")
	_ = os.MkdirAll(data, 0o755)
	return &Client{root: root, data: data, http: http.DefaultClient, st: State{Server: srv, Token: "tok"}}
}

func TestSelfUpdateInstallsTheServersRelease(t *testing.T) {
	old := version
	version = "1.0.0"
	defer func() { version = old }()
	files := map[string]string{appName: "new binary", "app.json": `{"version":"9.0.0"}`, "icon.png": "new icon"}
	c := updateClient(t, updateServer(t, files, "").URL)
	u, err := c.CheckUpdate()
	if err != nil || !u.Available || u.Version != "9.0.0" || len(u.Files) != 3 {
		t.Fatalf("%+v %v", u, err)
	}
	if err := c.InstallUpdate(u); err != nil {
		t.Fatal(err)
	}
	for name, body := range files {
		b, _ := os.ReadFile(filepath.Join(c.appDir(), name))
		if string(b) != body {
			t.Fatalf("%s: %q", name, b)
		}
	}
	if fi, _ := os.Stat(filepath.Join(c.appDir(), appName)); fi.Mode()&0o100 == 0 {
		t.Fatal("binary not executable")
	}
	if _, err := os.Stat(filepath.Join(c.data, "update")); !os.IsNotExist(err) {
		t.Fatal("staging folder left behind")
	}
	version = "9.0.0"
	if u, err := c.CheckUpdate(); err != nil || u.Available {
		t.Fatalf("same version offered: %+v %v", u, err)
	}
	version = "dev"
	if u, _ := c.CheckUpdate(); u.Available {
		t.Fatal("a development build must not update")
	}
}

func TestADamagedUpdateLeavesTheAppUnchanged(t *testing.T) {
	old := version
	version = "1.0.0"
	defer func() { version = old }()
	files := map[string]string{appName: "new binary", "icon.png": "new icon"}
	c := updateClient(t, updateServer(t, files, appName).URL)
	u, err := c.CheckUpdate()
	if err != nil {
		t.Fatal(err)
	}
	if err := c.InstallUpdate(u); err == nil {
		t.Fatal("damaged download installed")
	}
	for name, body := range map[string]string{appName: "old binary", "icon.png": "old icon"} {
		if b, _ := os.ReadFile(filepath.Join(c.appDir(), name)); string(b) != body {
			t.Fatalf("%s changed: %q", name, b)
		}
	}
	u.Files[0].Path = "../escape"
	if err := c.InstallUpdate(u); err == nil {
		t.Fatal("unsafe path accepted")
	}
}
