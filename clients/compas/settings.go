// SPDX-License-Identifier: GPL-3.0-or-later
// The app's own settings (screen timers, what to do after a sync), kept in $COMPAS_APP_DATA/settings.json.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"time"
)

// after-sync actions
const (
	afterAsk      = "ask"
	afterReboot   = "reboot"
	afterPowerOff = "poweroff"
	afterNothing  = "nothing"
)

type Settings struct {
	DimAfterS   int    `json:"dim_after_s"`   // 0 = never
	DimPercent  int    `json:"dim_percent"`   // brightness while dimmed, % of the normal brightness
	OffAfterS   int    `json:"off_after_s"`   // 0 = never; counted from the last input like the dim timer
	AfterSync   string `json:"after_sync"`    // ask | reboot | poweroff | nothing
	KeepAwake   bool   `json:"keep_awake"`    // never dim or switch the screen off while a sync runs
	MinBattery  int    `json:"min_battery"`   // % a sync needs to start (ignored while charging)
	SyncOnStart bool   `json:"sync_on_start"` // start a sync when the app opens
}

func defaultSettings() Settings {
	return Settings{DimAfterS: 30, DimPercent: 20, OffAfterS: 120, AfterSync: afterAsk, KeepAwake: true, MinBattery: 20}
}

// the values a setting cycles through when it is selected
var (
	dimChoices     = []int{0, 15, 30, 60, 120}
	percentChoices = []int{5, 10, 20, 40}
	offChoices     = []int{0, 30, 60, 120, 300, 600}
	afterChoices   = []string{afterAsk, afterReboot, afterPowerOff, afterNothing}
	batteryChoices = []int{0, 10, 20, 30, 50}
)

// batteryFloor: below this a running sync stops at the next safe point (unless the player charges)
const batteryFloor = 10

// batteryBlocks: why a sync must not start (or go on) now: "" = fine. start: the setting's minimum applies,
// else the hard floor. An unknown level never blocks.
func batteryBlocks(s Settings, pct int, charging, start bool) bool {
	if pct < 0 || charging {
		return false
	}
	if start {
		return pct < s.MinBattery
	}
	return pct < batteryFloor
}

// History keeps the last syncs (newest first).
type HistoryEntry struct {
	At      string `json:"at"`
	Title   string `json:"title"`
	Summary string `json:"summary"`
}

const historyLen = 10

func addHistory(h []HistoryEntry, e HistoryEntry) []HistoryEntry {
	h = append([]HistoryEntry{e}, h...)
	if len(h) > historyLen {
		h = h[:historyLen]
	}
	return h
}

func next[T comparable](list []T, cur T) T {
	for i, v := range list {
		if v == cur {
			return list[(i+1)%len(list)]
		}
	}
	return list[0]
}

func loadSettings(dir string) Settings {
	s := defaultSettings()
	if b, err := os.ReadFile(filepath.Join(dir, "settings.json")); err == nil {
		_ = json.Unmarshal(b, &s)
	}
	return s
}

func (s Settings) save(dir string) error {
	b, _ := json.MarshalIndent(s, "", " ")
	return writeAtomic(filepath.Join(dir, "settings.json"), b)
}

// restoreSettings, after pairing: the settings the server kept for this player (it paired before, e.g. after
// a reset of the app) replace the local ones; a server without a copy gets ours.
func (c *Client) restoreSettings(raw json.RawMessage) {
	var kept map[string]any
	if json.Unmarshal(raw, &kept) == nil && len(kept) > 0 {
		s := defaultSettings()
		if json.Unmarshal(raw, &s) == nil {
			if !validAfter(s.AfterSync) {
				s.AfterSync = afterAsk
			}
			if err := s.save(c.data); err == nil {
				step("settings: restored from the server")
				return
			}
		}
	}
	c.pushSettings(loadSettings(c.data))
}

func validAfter(v string) bool {
	for _, a := range afterChoices {
		if a == v {
			return true
		}
	}
	return false
}

// pushSettings gives the server a copy of the app's settings (best effort: it is sent again on the next change).
func (c *Client) pushSettings(s Settings) {
	if c.st.Token == "" {
		return
	}
	if err := c.call("PUT", "me/settings", map[string]any{"settings": s}, nil); err != nil {
		step("settings: not sent to the server: %v", err)
	}
}

func seconds(n int) string {
	switch {
	case n == 0:
		return T("set.never")
	case n%60 == 0:
		return fmt.Sprintf("%d min", n/60)
	}
	return fmt.Sprintf("%d s", n)
}

// screen brightness states
const (
	screenOn = iota
	screenDim
	screenOff
)

// screenMode: what the screen should be after `idle` without input, given its current mode `cur`. Dimming and
// switching off count from the same last input; an off screen stays off until input wakes it (handled by the
// caller), so a dim timer longer than the off timer never lights it again. A running sync keeps the screen as it
// is when KeepAwake is set (the default): neither timer applies.
func screenMode(s Settings, cur int, idle time.Duration, syncing bool) int {
	if cur == screenOff {
		return screenOff
	}
	if syncing && s.KeepAwake {
		return cur
	}
	if s.OffAfterS > 0 && idle >= time.Duration(s.OffAfterS)*time.Second {
		return screenOff
	}
	if s.DimAfterS > 0 && idle >= time.Duration(s.DimAfterS)*time.Second {
		return screenDim
	}
	return screenOn
}

// dimAfterOff: the dim timer can never act because the screen switches off first.
func (s Settings) dimAfterOff() bool {
	return s.DimAfterS > 0 && s.OffAfterS > 0 && s.DimAfterS >= s.OffAfterS
}

// combo detects the forced exit: volume up held together with volume down or with next/previous for `hold`.
type combo struct {
	up, down bool
	skip     bool      // next or previous held
	since    time.Time // when both went down; zero = not both held
	hold     time.Duration
}

func (k *combo) feed(code uint16, down bool, now time.Time) {
	switch code {
	case keyVolUp:
		k.up = down
	case keyVolDown:
		k.down = down
	case keyNext, keyPrev:
		k.skip = down
	default:
		return
	}
	if k.up && (k.down || k.skip) {
		if k.since.IsZero() {
			k.since = now
		}
	} else {
		k.since = time.Time{}
	}
}

func (k *combo) fired(now time.Time) bool {
	return !k.since.IsZero() && now.Sub(k.since) >= k.hold
}
