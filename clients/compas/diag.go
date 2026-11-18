// SPDX-License-Identifier: GPL-3.0-or-later
// Diagnostics: the app's log (.apps/.last-session.log) and a summary of its state for the server, without the
// token.
package main

import (
	"os"
	"path/filepath"
	"strings"
)

const diagMax = 400 * 1024 // the end of the log; the server takes at most 512 KiB

// diagnostics: what "Send diagnostics" uploads.
func (c *Client) diagnostics(set Settings) map[string]any {
	log, _ := os.ReadFile(filepath.Join(c.root, ".apps", ".last-session.log"))
	if len(log) > diagMax {
		log = log[len(log)-diagMax:]
	}
	text := string(log)
	if c.st.Token != "" {
		text = strings.ReplaceAll(text, c.st.Token, "[redacted]")
	}
	total, free := freeSpace(c.root)
	pct, charging := battery()
	return map[string]any{
		"log": text,
		"summary": map[string]any{
			"app_version": version, "server": c.st.Server, "device_id": c.st.DeviceID,
			"files": len(c.st.Files), "events_waiting": len(c.st.Events), "playlists": len(c.st.Lists),
			"history": c.st.History, "settings": set,
			"card_total": total, "card_free": free, "battery": pct, "charging": charging,
		},
	}
}

// SendDiagnostics uploads them.
func (c *Client) SendDiagnostics(set Settings) error {
	return c.call("POST", "diagnostics", c.diagnostics(set), nil)
}

// Template is one of the server's templates and whether this player uses it.
type Template struct {
	ID   string `json:"id"`
	Name string `json:"name"`
	On   bool   `json:"on"`
}

// Templates reads them; SetTemplates switches them (the ids that are on) and returns the new list.
func (c *Client) Templates() ([]Template, error) {
	var out struct {
		Templates []Template `json:"templates"`
	}
	err := c.call("GET", "templates", nil, &out)
	return out.Templates, err
}

func (c *Client) SetTemplates(on []string) ([]Template, error) {
	var out struct {
		Templates []Template `json:"templates"`
	}
	err := c.call("PUT", "templates", map[string]any{"on": on}, &out)
	return out.Templates, err
}
