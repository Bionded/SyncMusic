// SPDX-License-Identifier: GPL-3.0-or-later
package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func openIn(t *testing.T, data string) *Client {
	t.Helper()
	t.Setenv("COMPAS_APP_DATA", data)
	t.Setenv("SYNCMUSIC_SD_ROOT", t.TempDir())
	c, err := open()
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	return c
}

func TestDamagedStateStartsUnpairedAndKeepsTheFileAside(t *testing.T) {
	for name, body := range map[string][]byte{
		"garbage":   []byte("AALIAS1\x00\x01\x02\xff\xfe binary"),
		"empty":     {},
		"truncated": []byte(`{"uid": "u1", "server": "http://x/", "tok`),
	} {
		t.Run(name, func(t *testing.T) {
			data := t.TempDir()
			if err := os.WriteFile(filepath.Join(data, "state.json"), body, 0o644); err != nil {
				t.Fatal(err)
			}
			c := openIn(t, data)
			if c.Damaged == "" || c.st.Token != "" || c.st.UID == "" {
				t.Fatalf("want a fresh unpaired state with the damage noted, got %+v damaged=%q", c.st, c.Damaged)
			}
			if !strings.Contains(c.Damaged, "state.json.broken-") {
				t.Fatalf("broken file not moved aside: %q", c.Damaged)
			}
			if _, err := os.Stat(c.Damaged); err != nil {
				t.Fatalf("aside copy missing: %v", err)
			}
			a := &App{c: c}
			if p := a.pairMenu(); p.title != T("state.damaged.title") {
				t.Fatalf("pair screen does not say why: %q", p.title)
			}
			if err := c.save(); err != nil {
				t.Fatal(err)
			}
			if again := openIn(t, data); again.Damaged != "" || again.st.UID != c.st.UID {
				t.Fatalf("a saved fresh state must read back: %+v", again)
			}
		})
	}
}

func TestBackupStateIsTheFallback(t *testing.T) {
	first, second := "pairing-one", "pairing-two" // made-up values
	data := t.TempDir()
	c := openIn(t, data)
	c.st.Server = "http://x/"
	c.st.Token = first
	if err := c.save(); err != nil {
		t.Fatal(err)
	}
	c.st.Token = second
	if err := c.save(); err != nil { // the first save becomes state.json.bak
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(data, "state.json"), []byte("\x00garbage"), 0o644); err != nil {
		t.Fatal(err)
	}
	again := openIn(t, data)
	if again.Damaged != "" || again.st.Token != first || again.st.UID != c.st.UID {
		t.Fatalf("want the .bak state, got %+v damaged=%q", again.st, again.Damaged)
	}
	// nothing written into the working folder (the app folder on the card)
	wd, _ := os.Getwd()
	if _, err := os.Stat(filepath.Join(wd, "state.json")); err == nil {
		t.Fatal("state.json written into the working directory")
	}
}
