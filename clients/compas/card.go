package main

import (
	"bufio"
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
	tmp := filepath.Join(dir, "manifest.jsonl.tmp")
	f, err := os.Create(tmp)
	if err != nil {
		return ""
	}
	_, werr := f.Write(append(head, '\n'))
	serr := f.Sync()
	if f.Close() != nil || werr != nil || serr != nil || os.Rename(tmp, filepath.Join(dir, "manifest.jsonl")) != nil {
		os.Remove(tmp)
		return ""
	}
	return id
}
