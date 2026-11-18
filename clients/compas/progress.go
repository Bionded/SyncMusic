// SPDX-License-Identifier: GPL-3.0-or-later
// What a running sync is doing, for the sync page: phase, files and bytes, speed, time left, counters.
package main

import (
	"fmt"
	"path"
	"strings"
	"sync"
	"time"
)

// sync phases in order
const (
	phaseSend      = "send"      // plays, hearts, positions and playlist edits go up
	phasePlan      = "plan"      // the server works out what to sync
	phaseRemove    = "remove"    // files the server no longer wants
	phaseDownload  = "download"  // new and changed files
	phaseDatabase  = "database"  // the player's library built by the server is installed
	phasePositions = "positions" // hearts, play counts and positions go to the player
)

var phaseOrder = []string{phaseSend, phasePlan, phaseRemove, phaseDownload, phaseDatabase, phasePositions}

type Progress struct {
	Phase             string
	Files, FilesTotal int
	Bytes, BytesTotal int64
	File              string // the file being downloaded (card path)
	FileSize          int64
	Started           time.Time // when the download phase began
	Received, Removed int
	Playlists         int
	Sent              map[string]int
	HeartsDown        int
	PositionsDown     int
}

type progressBox struct {
	mu sync.Mutex
	p  Progress
}

func (b *progressBox) update(f func(p *Progress)) {
	b.mu.Lock()
	f(&b.p)
	b.mu.Unlock()
}

func (b *progressBox) snapshot() Progress {
	b.mu.Lock()
	defer b.mu.Unlock()
	p := b.p
	p.Sent = map[string]int{}
	for k, v := range b.p.Sent {
		p.Sent[k] = v
	}
	return p
}

// countWriter adds what is written to the sync's byte counter.
type countWriter struct{ b *progressBox }

func (w countWriter) Write(p []byte) (int, error) {
	w.b.update(func(pr *Progress) { pr.Bytes += int64(len(p)) })
	return len(p), nil
}

func mb(n int64) string {
	if n >= 1e9 {
		return fmt.Sprintf("%.2f GB", float64(n)/1e9)
	}
	return fmt.Sprintf("%.1f MB", float64(n)/1e6)
}

// rate and time left of the download so far; ok is false until there is enough to tell.
func rateETA(p Progress, now time.Time) (bps float64, left time.Duration, ok bool) {
	el := now.Sub(p.Started).Seconds()
	if p.Started.IsZero() || el < 1 || p.Bytes <= 0 {
		return 0, 0, false
	}
	bps = float64(p.Bytes) / el
	return bps, time.Duration(float64(p.BytesTotal-p.Bytes)/bps) * time.Second, true
}

func duration(d time.Duration) string {
	switch s := int(d.Seconds()); {
	case s < 60:
		return fmt.Sprintf("%d s", max(s, 1))
	case s < 3600:
		return fmt.Sprintf("%d min", (s+59)/60)
	default:
		return fmt.Sprintf("%d h %d min", s/3600, s%3600/60)
	}
}

// syncLines: the sync page for a progress snapshot: the phase bar with the phases' names, the file being
// downloaded (a card), files done of all with the byte bar and done / speed / time left (a card), and the counters.
func syncLines(p Progress, now time.Time) []line {
	step := 1
	names := []string{}
	for i, ph := range phaseOrder {
		if ph == p.Phase {
			step = i + 1
		}
		names = append(names, T("phase_s."+ph))
	}
	ls := []line{{steps: []int{step, len(phaseOrder)}, names: names}}
	if p.Phase != phaseDownload {
		ls = append(ls, line{text: T("phase." + p.Phase), size: sizeHead, box: boxCard})
	}
	if p.Phase == phaseDownload {
		if p.File != "" {
			title, sub := fileInfo(p.File)
			sub = append(sub, strings.ToUpper(strings.TrimPrefix(path.Ext(p.File), "."))+" "+mbShort(p.FileSize))
			ls = append(ls, line{text: T("sync.now_l"), size: sizeLabel, col: colDim, box: boxCard},
				line{text: title, size: sizeHead, box: boxCard},
				line{text: strings.Join(sub, " · "), size: sizeSmall, col: colSoft, box: boxCard})
		}
		speed, left := "–", "–"
		if bps, l, ok := rateETA(p, now); ok {
			speed, left = mbShort(int64(bps))+"/s", "~"+duration(l)
		}
		ls = append(ls, line{text: fmt.Sprintf("%d / %d", p.Files, p.FilesTotal), right: T("sync.files"), rcol: colSoft, size: sizeBig, box: boxCard2},
			line{bar: []int64{p.Bytes, p.BytesTotal}, box: boxCard2},
			line{stats: []stat{{T("sync.done"), mbShort(p.Bytes) + " / " + mbShort(p.BytesTotal)}, {T("sync.speed_l"), speed},
				{T("sync.left"), left}}, box: boxCard2})
	}
	ls = append(ls, line{grid: []string{T("sync.received", p.Received), T("sync.removed_n", p.Removed),
		T("sync.plays_n", p.Sent["play"]), T("sync.hearts_n", p.Sent["favourite"]),
		T("sync.lists_n", p.Playlists), T("sync.pos_n", p.Sent["position"])}, col: colSoft})
	if n := p.Sent["playlist_edit"]; n > 0 {
		ls = append(ls, line{text: T("sync.sent_lists", Count(n, "playlist edit", "playlist edits")), col: colDim, size: sizeSmall})
	}
	if p.HeartsDown+p.PositionsDown > 0 {
		ls = append(ls, line{text: T("sync.down", Count(p.HeartsDown, "heart or play count", "hearts and play counts"),
			Count(p.PositionsDown, "position", "positions")), col: colDim, size: sizeSmall})
	}
	return ls
}

// fileInfo: a card path "Music/Artist/Album/07 Title.flac" as the title and its folders (artist, album).
func fileInfo(p string) (string, []string) {
	base := strings.TrimSuffix(path.Base(p), path.Ext(p))
	if i := strings.IndexByte(base, ' '); i > 0 && strings.Trim(base[:i], "0123456789-.") == "" {
		base = base[i+1:]
	}
	parts := strings.Split(path.Dir(p), "/")
	if len(parts) > 2 {
		parts = parts[len(parts)-2:]
	} else if len(parts) > 0 && (parts[0] == "." || parts[0] == "Music") {
		parts = parts[1:]
	}
	return base, parts
}

// mbShort: "31 MB", "1.4 GB".
func mbShort(n int64) string {
	switch {
	case n >= 1e9:
		return fmt.Sprintf("%.1f GB", float64(n)/1e9)
	case n >= 10e6:
		return fmt.Sprintf("%d MB", n/1e6)
	}
	return fmt.Sprintf("%.1f MB", float64(n)/1e6)
}

// flat is a page's blocks as plain text (the session log and tests): text lines as they are, tiles as
// "label value" joined by " · ".
func flat(ls []line) []string {
	out := []string{}
	for _, l := range ls {
		switch {
		case len(l.stats) > 0:
			ts := []string{}
			for _, st := range l.stats {
				ts = append(ts, st.label+" "+st.value)
			}
			out = append(out, strings.Join(ts, " · "))
		case len(l.grid) > 0:
			out = append(out, strings.Join(l.grid, " · "))
		case len(l.steps) == 2 && len(l.names) >= l.steps[0] && l.steps[0] > 0:
			out = append(out, fmt.Sprintf("%s (%d/%d)", l.names[l.steps[0]-1], l.steps[0], l.steps[1]))
		case l.text != "" && l.right != "":
			out = append(out, l.text+" "+l.right)
		case l.text != "":
			out = append(out, l.text)
		}
	}
	return out
}
