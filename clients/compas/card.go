package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"time"
)

// The card's identity is the id in the header of SyncMusic's manifest on the card (the same file a USB sync
// writes): the card is the device, whichever player or reader it sits in.
const manifestDir = ".syncmusic"

func readCardID(root string) string {
	for _, name := range []string{"manifest.jsonl", "manifest.jsonl.bak"} {
		f, err := os.Open(filepath.Join(root, manifestDir, name))
		if err != nil {
			continue
		}
		line, _ := bufio.NewReader(f).ReadBytes('\n')
		f.Close()
		var h struct {
			Type      string `json:"type"`
			DeviceID  string `json:"device_id"`
			DeviceUID string `json:"device_uid"`
		}
		if json.Unmarshal(line, &h) != nil || h.Type != "header" {
			continue
		}
		if h.DeviceUID != "" {
			return h.DeviceUID
		}
		if h.DeviceID != "" {
			return h.DeviceID
		}
	}
	return ""
}

// cardID returns the card's id; a card SyncMusic never wrote to gets a manifest with only a header (no files).
// A manifest whose header cannot be read is kept aside (manifest.jsonl.broken-<time>) and its rows are carried
// into the new one, so the files it lists stay known.
func (c *Client) cardID() string {
	if c.root == "" {
		return "" // card root unknown: never write a manifest into the working directory
	}
	if id := readCardID(c.root); id != "" {
		return id
	}
	id := uuid4()
	now := time.Now().UTC().Format(time.RFC3339)
	head, _ := json.Marshal(map[string]any{"type": "header", "manifest_version": 2, "app_version": "syncmusic-compas " + version,
		"device_id": id, "device_uid": id, "created_at": now, "updated_at": now})
	dir := filepath.Join(c.root, manifestDir)
	if os.MkdirAll(dir, 0o755) != nil {
		return ""
	}
	body := append(head, '\n')
	cur := filepath.Join(dir, "manifest.jsonl")
	if old, err := os.ReadFile(cur); err == nil {
		aside := cur + ".broken-" + time.Now().UTC().Format("20060102T150405Z")
		if os.Rename(cur, aside) != nil {
			return "" // never write over a manifest that could not be kept
		}
		body = append(body, manifestRows(old)...)
	}
	tmp := filepath.Join(dir, "manifest.jsonl.tmp")
	f, err := os.Create(tmp)
	if err != nil {
		return ""
	}
	_, werr := f.Write(body)
	serr := f.Sync()
	if f.Close() != nil || werr != nil || serr != nil || os.Rename(tmp, filepath.Join(dir, "manifest.jsonl")) != nil {
		os.Remove(tmp)
		return ""
	}
	return id
}

// manifestRows returns the lines of a manifest that are readable rows other than a header.
func manifestRows(data []byte) []byte {
	var out []byte
	sc := bufio.NewScanner(bytes.NewReader(data))
	sc.Buffer(make([]byte, 1<<20), 16<<20)
	for sc.Scan() {
		var row struct {
			Type string `json:"type"`
		}
		if json.Unmarshal(sc.Bytes(), &row) != nil || row.Type == "" || row.Type == "header" {
			continue
		}
		out = append(append(out, sc.Bytes()...), '\n')
	}
	return out
}

// readCardFiles returns the paths SyncMusic's manifest on the card lists (tracks and playlists): files a USB sync
// (or an earlier app) wrote. They are SyncMusic's own files, so the app reports them as held like its own downloads;
// without them a first Wi-Fi sync after a USB sync would send the whole card again. Nil when there is no manifest.
func readCardFiles(root string) []string {
	if root == "" {
		return nil
	}
	for _, name := range []string{"manifest.jsonl", "manifest.jsonl.bak"} {
		f, err := os.Open(filepath.Join(root, manifestDir, name))
		if err != nil {
			continue
		}
		items := map[string]string{} // lib/item_id -> path
		lists := map[string]string{} // playlist name -> path
		sc := bufio.NewScanner(f)
		sc.Buffer(make([]byte, 1<<20), 16<<20)
		header := false
		for sc.Scan() {
			var row struct {
				Type   string `json:"type"`
				Lib    string `json:"lib"`
				ItemID string `json:"item_id"`
				Path   string `json:"path"`
				Name   string `json:"name"`
			}
			if json.Unmarshal(sc.Bytes(), &row) != nil {
				continue
			}
			switch row.Type {
			case "header":
				header = true
			case "item":
				if row.Path != "" {
					items[row.Lib+"/"+row.ItemID] = row.Path
				}
			case "delete":
				delete(items, row.Lib+"/"+row.ItemID)
			case "playlist":
				if row.Path != "" {
					lists[row.Name] = row.Path
				}
			}
		}
		f.Close()
		if !header || sc.Err() != nil {
			continue
		}
		out := make([]string, 0, len(items)+len(lists))
		for _, p := range items {
			out = append(out, p)
		}
		for _, p := range lists {
			out = append(out, p)
		}
		return out
	}
	return nil
}
