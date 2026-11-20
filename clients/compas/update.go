// SPDX-License-Identifier: GPL-3.0-or-later
package main

// Self-update: the server offers the files of a newer release of this app (GET app-update); they are downloaded
// into a staging folder, checked (size + SHA-256), then moved into .apps/syncmusic/ one by one with the program
// file last. Before the first move the list of files is written to update.pending (next to the staging folder);
// a power cut during the moves leaves that list and the staged files, and the next start finishes the moves
// (FinishUpdate), so the app never stays a mix of old and new files.

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
)

const appName = "syncmusic-compas"

type appFile struct {
	Path   string `json:"path"`
	Size   int64  `json:"size"`
	Sha256 string `json:"sha256"`
	URL    string `json:"url"`
}

type appUpdate struct {
	Available     bool      `json:"available"`
	Version       string    `json:"version"`
	Files         []appFile `json:"files"`
	ServerVersion string    `json:"server_version"` // the newest app the server holds ("" = none or an older server)
	ClientNewer   bool      `json:"client_newer"`   // this app is newer than the server's: update the server
}

// AnswerUpdate tells the server what the user chose on the update prompt ("update" or "later"); an older
// server without the endpoint is ignored.
func (c *Client) AnswerUpdate(v, answer string) {
	_ = c.call("POST", "app-update/answer", map[string]string{"version": v, "answer": answer}, nil)
}

// appDir: where the app is installed on the card.
func (c *Client) appDir() string { return filepath.Join(c.root, ".apps", "syncmusic") }

// CheckUpdate asks the server for a newer release of the app; a development build never updates.
func (c *Client) CheckUpdate() (appUpdate, error) {
	var u appUpdate
	if version == "dev" || c.st.Token == "" {
		return u, nil
	}
	q := url.Values{"app": {appName}, "version": {version}, "variant": {floatABI}}
	err := c.call("GET", "app-update?"+q.Encode(), nil, &u)
	if err == nil && u.Available && len(u.Files) == 0 {
		u.Available = false
	}
	return u, err
}

// InstallUpdate downloads every file of u and installs them, the program file last.
func (c *Client) InstallUpdate(u appUpdate) error {
	stage := filepath.Join(c.data, "update")
	_ = os.RemoveAll(stage)
	_ = os.Remove(c.pendingPath())
	var exe *appFile
	for i, f := range u.Files {
		if !safeRel(f.Path) {
			_ = os.RemoveAll(stage)
			return fmt.Errorf("update: bad file name %q", f.Path)
		}
		if err := c.fetchTo(f, filepath.Join(stage, filepath.FromSlash(f.Path))); err != nil {
			_ = os.RemoveAll(stage)
			return err
		}
		if f.Path == appName {
			exe = &u.Files[i]
		}
	}
	if exe == nil {
		_ = os.RemoveAll(stage)
		return fmt.Errorf("update: the release has no %s", appName)
	}
	var paths []string
	for _, f := range append(others(u.Files, exe.Path), *exe) {
		paths = append(paths, f.Path)
	}
	b, _ := json.Marshal(paths)
	if err := writeSynced(c.pendingPath(), b); err != nil {
		_ = os.RemoveAll(stage)
		return err
	}
	syncDir(c.data)
	return c.moveStaged(paths)
}

func (c *Client) pendingPath() string { return filepath.Join(c.data, "update.pending") }

// FinishUpdate completes an update a power cut interrupted while its files were being moved into place; it does
// nothing when no update is pending. Called at start, before the app reads its own files.
func (c *Client) FinishUpdate() error {
	b, err := os.ReadFile(c.pendingPath())
	if err != nil {
		_ = os.RemoveAll(filepath.Join(c.data, "update")) // a download cut short: nothing was moved yet
		return nil
	}
	var paths []string
	if json.Unmarshal(b, &paths) != nil {
		_ = os.Remove(c.pendingPath())
		_ = os.RemoveAll(filepath.Join(c.data, "update"))
		return fmt.Errorf("update: damaged update.pending dropped")
	}
	return c.moveStaged(paths)
}

// moveStaged moves the staged files listed in paths into the app folder (a file already moved is skipped),
// syncs the folders and then drops the pending list and the staging folder.
func (c *Client) moveStaged(paths []string) error {
	stage, dir := filepath.Join(c.data, "update"), c.appDir()
	dirs := map[string]bool{}
	for _, p := range paths {
		if !safeRel(p) {
			continue
		}
		src, dst := filepath.Join(stage, filepath.FromSlash(p)), filepath.Join(dir, filepath.FromSlash(p))
		if _, err := os.Stat(src); err != nil {
			continue // moved before the cut
		}
		if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
			return err
		}
		if err := os.Rename(src, dst); err != nil {
			return err
		}
		dirs[filepath.Dir(dst)] = true
		if p == appName {
			if err := os.Chmod(dst, 0o755); err != nil {
				return err
			}
		}
	}
	for d := range dirs {
		syncDir(d)
	}
	_ = os.Remove(c.pendingPath())
	syncDir(c.data)
	return os.RemoveAll(stage)
}

func others(fs []appFile, skip string) []appFile {
	var out []appFile
	for _, f := range fs {
		if f.Path != skip {
			out = append(out, f)
		}
	}
	return out
}

func safeRel(p string) bool {
	if p == "" || strings.HasPrefix(p, "/") || strings.Contains(p, "\\") {
		return false
	}
	for _, part := range strings.Split(p, "/") {
		if part == "" || part == "." || part == ".." {
			return false
		}
	}
	return true
}

// fetchTo downloads one file of the release to dest and checks its size and SHA-256.
func (c *Client) fetchTo(f appFile, dest string) error {
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		return err
	}
	req, _ := http.NewRequest("GET", c.url(f.URL), nil)
	req.Header.Set("Authorization", "Bearer "+c.st.Token)
	r, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer r.Body.Close()
	if r.StatusCode >= 400 {
		return &ProtoError{r.StatusCode, "download", f.Path}
	}
	out, err := os.Create(dest)
	if err != nil {
		return err
	}
	h := sha256.New()
	n, err := io.Copy(io.MultiWriter(out, h), r.Body)
	if err == nil {
		err = out.Sync()
	}
	out.Close()
	if err != nil {
		return err
	}
	if n != f.Size || hex.EncodeToString(h.Sum(nil)) != f.Sha256 {
		return fmt.Errorf("update: %s arrived damaged", f.Path)
	}
	return nil
}
