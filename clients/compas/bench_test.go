// SPDX-License-Identifier: GPL-3.0-or-later
package main

import (
	"testing"
	"time"
)

// benchApp: an app with a 480x800 screen and filled state, for frame-time benchmarks.
func benchApp(b *testing.B) *App {
	f, err := loadFonts()
	if err != nil {
		b.Fatal(err)
	}
	a := &App{c: &Client{root: b.TempDir(), st: State{Server: "http://192.0.2.10:8080/", DeviceID: "c0ffee12",
		LastSync: "2026-11-16 21:40", LastSummary: "Received: 12 new, 3 removed."}}, set: defaultSettings(),
		s: &screen{w: 480, h: 800, pix: make([]uint16, 480*800), out: make([]byte, 480*800*2), stride: 960, font: f}}
	a.online.Store(ptr(T("home.online")))
	a.c.st.LastGrid = endGrid(Result{Fetched: 12, Deleted: 3, Sent: map[string]int{"play": 5}}, &downPlan{}, 1)
	for range 10 {
		a.c.st.History = append(a.c.st.History, HistoryEntry{"2026-11-16 21:40", T("end.finished"), "Received: 12 new, 3 removed."})
	}
	return a
}

func benchPage(b *testing.B, build func(a *App) page) {
	a := benchApp(b)
	p := build(a)
	b.ResetTimer()
	for range b.N {
		a.s.drawPage(p, 0, "Wi-Fi ●   64%")
	}
}

func BenchmarkHome(b *testing.B)     { benchPage(b, func(a *App) page { return a.home() }) }
func BenchmarkHistory(b *testing.B)  { benchPage(b, func(a *App) page { return a.history() }) }
func BenchmarkSettings(b *testing.B) { benchPage(b, func(a *App) page { return a.settings() }) }
func BenchmarkSync(b *testing.B) {
	t0 := time.Now()
	p := Progress{Phase: phaseDownload, Files: 37, FilesTotal: 412, Bytes: 912e6, BytesTotal: 9.8e9,
		File: "Music/a/b.flac", FileSize: 38.2e6, Started: t0.Add(-7 * time.Minute)}
	benchPage(b, func(a *App) page {
		return page{title: T("sync.title_short"), showStatus: true, lines: syncLines(p, t0),
			buttons: []button{{label: T("btn.stop"), kind: btnDanger}}}
	})
}

// TestCachedDrawingSamePixels: drawing a page again (corners, logo, icons, toggles from the caches) gives exactly
// the pixels of a first draw on a fresh screen.
func TestCachedDrawingSamePixels(t *testing.T) {
	f, err := loadFonts()
	if err != nil {
		t.Fatal(err)
	}
	a := &App{c: &Client{root: t.TempDir(), st: State{Server: "http://192.0.2.10:8080/"}}, set: defaultSettings()}
	a.online.Store(ptr(T("home.online")))
	pages := []page{a.home(), a.settings(), {title: "Done", icon: iconCheck, centered: true}, {title: "Error", icon: iconError}}
	for _, p := range pages {
		fresh := &screen{w: 480, h: 800, pix: make([]uint16, 480*800), out: make([]byte, 480*800*2), stride: 960, font: f}
		fresh.drawPage(p, 0, "64%")
		want := append([]uint16(nil), fresh.pix...)
		fresh.drawPage(pages[0], 0, "64%") // something else in between
		fresh.drawPage(p, 0, "64%")
		for i := range want {
			if want[i] != fresh.pix[i] {
				t.Fatalf("%s: pixel %d,%d differs after a cached redraw", p.title, i%480, i/480)
			}
		}
	}
}

// TestIdleTickDoesNotRedraw: render skips drawing when nothing on the screen changed, and draws again on a change.
func TestIdleTickDoesNotRedraw(t *testing.T) {
	f, err := loadFonts()
	if err != nil {
		t.Fatal(err)
	}
	s := &screen{w: 480, h: 800, pix: make([]uint16, 480*800), out: make([]byte, 480*800*2), stride: 960, font: f}
	txt := "one"
	a := &App{s: s, mode: screenOn, bl: &backlight{}, set: defaultSettings(), c: &Client{root: t.TempDir()}}
	a.go_(func() page {
		return page{title: "T", lines: []line{{text: txt}}, buttons: []button{{label: "A"}, {label: "B"}}}
	})
	a.render()
	s.pix[0] = 0xffff // a marker a redraw would paint over
	a.render()
	if s.pix[0] != 0xffff {
		t.Fatal("an unchanged frame was drawn again")
	}
	for _, change := range []func(){func() { txt = "two" }, func() { a.sel = 1 }, func() { s.swiped, s.scroll = true, 30 }} {
		change()
		s.pix[0] = 0xffff
		a.render()
		if s.pix[0] == 0xffff {
			t.Fatal("a changed frame was not drawn")
		}
	}
}

// BenchmarkSwipeHistory: a swipe frame on a long page (the content is copied, not drawn again).
func BenchmarkSwipeHistory(b *testing.B)       { benchSwipe(b, false) }
func BenchmarkSwipeHistoryDirect(b *testing.B) { benchSwipe(b, true) }

func benchSwipe(b *testing.B, direct bool) {
	noBlit = direct
	defer func() { noBlit = false }()
	a := benchApp(b)
	for range 20 {
		a.c.st.History = append(a.c.st.History, a.c.st.History[0])
	}
	p := a.history()
	a.s.swiped = true
	b.ResetTimer()
	for i := range b.N {
		a.s.scroll = i % 400
		a.s.drawPage(p, 0, "Wi-Fi ●   64%")
	}
}

// TestBlitScrollSamePixels: a scrolled long page drawn through the content surface gives the pixels of a direct draw.
func TestBlitScrollSamePixels(t *testing.T) {
	f, err := loadFonts()
	if err != nil {
		t.Fatal(err)
	}
	a := &App{c: &Client{root: t.TempDir(), st: State{Server: "http://192.0.2.10:8080/"}}, set: defaultSettings()}
	for range 30 {
		a.c.st.History = append(a.c.st.History, HistoryEntry{"2026-11-16 21:40", "Done", "Received: 12 new."})
	}
	for _, p := range []page{a.history(), a.settings()} {
		for _, sc := range []int{0, 37, 250, 100000} {
			draw := func(direct bool) []uint16 {
				noBlit = direct
				defer func() { noBlit = false }()
				s := &screen{w: 480, h: 800, pix: make([]uint16, 480*800), out: make([]byte, 480*800*2), stride: 960, font: f}
				s.swiped, s.scroll = true, sc
				s.drawPage(p, 1, "64%")
				s.scroll = sc
				s.drawPage(p, 1, "64%") // second frame reuses the surface
				return s.pix
			}
			want, got := draw(true), draw(false)
			for i := range want {
				if want[i] != got[i] {
					t.Fatalf("%s scroll %d: pixel %d,%d differs", p.title, sc, i%480, i/480)
				}
			}
		}
	}
}

func BenchmarkAnimateFrame(b *testing.B) {
	a := benchApp(b)
	now := time.Now()
	a.s.drawPage(page{title: "Sync", lines: syncLines(Progress{Phase: phaseDatabase, PhaseAt: now}, now)}, 0, "64%")
	b.ResetTimer()
	for i := range b.N {
		a.s.animate(now.Add(time.Duration(i) * animFrame))
	}
}
