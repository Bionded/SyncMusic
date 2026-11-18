// SPDX-License-Identifier: GPL-3.0-or-later
package main

import (
	"encoding/binary"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
	"unsafe"
)

func TestScreenModeTimersAndSync(t *testing.T) {
	sec := func(n int) time.Duration { return time.Duration(n) * time.Second }
	for _, c := range []struct {
		name     string
		dim, off int
		cur      int
		idle     time.Duration
		syncing  bool
		want     int
	}{
		{"dim<off on", 30, 120, screenOn, sec(10), false, screenOn},
		{"dim<off dims", 30, 120, screenOn, sec(30), false, screenDim},
		{"dim<off still dim", 30, 120, screenDim, sec(119), false, screenDim},
		{"dim<off off", 30, 120, screenDim, sec(120), false, screenOff},
		{"dim=off off wins", 60, 60, screenOn, sec(60), false, screenOff},
		{"dim>off off", 120, 60, screenOn, sec(60), false, screenOff},
		{"dim>off never re-lit", 120, 60, screenOff, sec(130), false, screenOff},
		{"dim disabled", 0, 60, screenOn, sec(59), false, screenOn},
		{"dim disabled off", 0, 60, screenOn, sec(60), false, screenOff},
		{"off disabled dims", 30, 0, screenOn, time.Hour, false, screenDim},
		{"never/never", 0, 0, screenOn, time.Hour, false, screenOn},
		{"sync keeps on", 30, 120, screenOn, time.Hour, true, screenOn},
		{"sync keeps dim", 30, 120, screenDim, time.Hour, true, screenDim},
		{"sync keeps off", 30, 120, screenOff, time.Hour, true, screenOff},
	} {
		s := Settings{DimAfterS: c.dim, OffAfterS: c.off, KeepAwake: true}
		if got := screenMode(s, c.cur, c.idle, c.syncing); got != c.want {
			t.Errorf("%s: %d, want %d", c.name, got, c.want)
		}
	}
	s := Settings{DimAfterS: 30, OffAfterS: 120}
	if screenMode(s, screenOn, time.Hour, true) != screenOff {
		t.Error("keep awake off: the timers apply during a sync")
	}
	if !(Settings{DimAfterS: 60, OffAfterS: 60}).dimAfterOff() || (Settings{DimAfterS: 30, OffAfterS: 60}).dimAfterOff() ||
		(Settings{DimAfterS: 120}).dimAfterOff() {
		t.Error("dimAfterOff")
	}
}

func TestForcedExitCombo(t *testing.T) {
	t0 := time.Unix(1000, 0)
	k := combo{hold: 3 * time.Second}
	k.feed(keyVolUp, true, t0)
	if k.fired(t0.Add(10 * time.Second)) {
		t.Fatal("one key alone must not fire")
	}
	k.feed(keyVolDown, true, t0.Add(time.Second))
	if k.fired(t0.Add(3 * time.Second)) {
		t.Fatal("fired before 3 s of both held")
	}
	if !k.fired(t0.Add(4 * time.Second)) {
		t.Fatal("both held 3 s: should fire")
	}
	k.feed(keyVolDown, false, t0.Add(5*time.Second))
	if k.fired(t0.Add(9 * time.Second)) {
		t.Fatal("a release resets the combo")
	}
	k.feed(keyPlayPause, true, t0.Add(6*time.Second)) // other keys do not matter
	k.feed(keyVolDown, true, t0.Add(6*time.Second))
	if !k.fired(t0.Add(9 * time.Second)) {
		t.Fatal("held again for 3 s: should fire")
	}
}

func TestForcedExitNextVolUp(t *testing.T) {
	t0 := time.Unix(1000, 0)
	for _, skip := range []uint16{keyNext, keyPrev} {
		k := combo{hold: 3 * time.Second}
		k.feed(skip, true, t0)
		k.feed(keyVolUp, true, t0.Add(time.Second))
		if k.fired(t0.Add(3 * time.Second)) {
			t.Fatal("fired before 3 s")
		}
		if !k.fired(t0.Add(4 * time.Second)) {
			t.Fatalf("key %d + volume up held 3 s: should fire", skip)
		}
		k.feed(skip, false, t0.Add(5*time.Second))
		if k.fired(t0.Add(9 * time.Second)) {
			t.Fatal("a release resets the combo")
		}
	}
}

func TestSettingsCycleAndPersist(t *testing.T) {
	dir := t.TempDir()
	s := loadSettings(dir)
	if s != defaultSettings() {
		t.Fatalf("defaults expected, got %+v", s)
	}
	if next(afterChoices, afterNothing) != afterAsk || next(dimChoices, 7) != 0 {
		t.Fatal("cycling wraps around and unknown values start over")
	}
	s.AfterSync, s.OffAfterS = afterPowerOff, 0
	if err := s.save(dir); err != nil {
		t.Fatal(err)
	}
	if got := loadSettings(dir); got != s {
		t.Fatalf("saved %+v, loaded %+v", s, got)
	}
}

func fakeSys(t *testing.T, files map[string]string) {
	root := t.TempDir()
	for p, v := range files {
		full := filepath.Join(root, p)
		_ = os.MkdirAll(filepath.Dir(full), 0o755)
		if err := os.WriteFile(full, []byte(v), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("SYNCMUSIC_SYSFS", root)
}

func TestBatteryPrefersTheBatterySupply(t *testing.T) {
	fakeSys(t, map[string]string{
		"sys/class/power_supply/usb/type":             "USB\n",
		"sys/class/power_supply/axp_battery/type":     "Battery\n",
		"sys/class/power_supply/axp_battery/capacity": "11\n",
		"sys/class/power_supply/battery/type":         "Battery\n",
		"sys/class/power_supply/battery/capacity":     "73\n",
		"sys/class/power_supply/battery/status":       "Charging\n",
	})
	if pct, ch := battery(); pct != 73 || !ch {
		t.Fatalf("got %d %v", pct, ch)
	}
}

func TestBacklightDimOffAndRestore(t *testing.T) {
	fakeSys(t, map[string]string{
		"sys/class/backlight/backlight_pwm0/brightness":     "200\n",
		"sys/class/backlight/backlight_pwm0/max_brightness": "255\n",
		"sys/class/graphics/fb0/blank":                      "0\n",
	})
	b := openBacklight()
	bright := func() string { return readTrim(filepath.Join(b.dir, "brightness")) }
	blank := func() string { return readTrim(sysRoot("/sys/class/graphics/fb0/blank")) }
	b.set(screenDim, 10)
	if bright() != "20" {
		t.Fatalf("dim: %s", bright())
	}
	b.set(screenOff, 10)
	if bright() != "0" || blank() != "1" {
		t.Fatalf("off: %s %s", bright(), blank())
	}
	b.restore()
	if bright() != "200" || blank() != "0" {
		t.Fatalf("restore: %s %s", bright(), blank())
	}
}

func TestPageNavigationTapsAndWake(t *testing.T) {
	fakeSys(t, nil)
	hits := []int{}
	a := &App{bl: &backlight{}, set: defaultSettings()}
	a.build = func() page {
		return page{buttons: []button{
			{label: "a", act: func() { hits = append(hits, 0) }},
			{label: "b", act: func() { hits = append(hits, 1) }},
		}}
	}
	a.rects = []rect{{0, 600, 480, 50}, {0, 660, 480, 50}}
	now := time.Now()
	press := func(code uint16) { a.handle(inEv{code: code, down: true}, now); a.handle(inEv{code: code}, now) }
	press(keyVolDown)
	press(keyVolDown) // stays on the last button
	press(keyPlayPause)
	press(keyVolUp)
	press(keyEnter)
	a.handle(inEv{tap: true, x: 100, y: 670}, now)
	a.handle(inEv{tap: true, x: 100, y: 10}, now) // outside every button
	a.mode = screenOff
	press(keyPlayPause) // only wakes the screen
	if a.mode != screenOn {
		t.Fatal("input should wake the screen")
	}
	if want := []int{1, 0, 1}; len(hits) != len(want) || hits[0] != 1 || hits[1] != 0 || hits[2] != 1 {
		t.Fatalf("hits %v, want %v", hits, want)
	}
}

func TestTouchAxisScaling(t *testing.T) {
	if (axis{0, 1023}).scale(512, 480) != 240 || (axis{}).scale(77, 480) != 77 {
		t.Fatal("scaling")
	}
}

func TestSyncLinesShowPhaseFilesSpeedAndCounters(t *testing.T) {
	t0 := time.Unix(5000, 0)
	p := Progress{Phase: phaseDownload, Files: 3, FilesTotal: 12, Bytes: 20e6, BytesTotal: 100e6,
		File: "Music/A/B/01 Перша.flac", FileSize: 31e6, Started: t0, Received: 3, Removed: 1, Playlists: 1,
		Sent: map[string]int{"play": 4, "favourite": 1}}
	ls := syncLines(p, t0.Add(10*time.Second))
	got := flat(ls)
	want := []string{
		"Download (4/6)",
		"Now",
		"Перша",
		"A · B · FLAC 31 MB",
		"3 / 12 files",
		"Done 20 MB / 100 MB · Speed 2.0 MB/s · Left ~40 s",
		"Received 3 · Removed 1 · Plays sent 4 · Hearts sent 1 · Playlists 1 · Positions 0",
	}
	if len(got) != len(want) {
		t.Fatalf("got %q", got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("line %d: %q, want %q", i, got[i], want[i])
		}
	}
	if _, _, ok := rateETA(Progress{Started: t0}, t0.Add(time.Second)); ok {
		t.Error("no bytes yet: no speed")
	}
	if duration(3725*time.Second) != "1 h 2 min" || duration(90*time.Second) != "2 min" {
		t.Error("durations")
	}
}

func TestByteCounterAndSnapshotCopy(t *testing.T) {
	var b progressBox
	b.update(func(p *Progress) { p.Sent = map[string]int{"play": 1} })
	_, _ = countWriter{&b}.Write(make([]byte, 1000))
	s := b.snapshot()
	s.Sent["play"] = 99
	if b.snapshot().Bytes != 1000 || b.snapshot().Sent["play"] != 1 {
		t.Fatal("counter or snapshot copy wrong")
	}
}

func TestWifiOnRunsThePlayersScriptThenDHCP(t *testing.T) {
	root := t.TempDir()
	t.Setenv("SYNCMUSIC_SYSFS", root)
	sock := filepath.Join(root, "var/run/wpa_supplicant/wlan0")
	lease := filepath.Join(root, "lease")
	if err := wifiOn(time.Second); err != errNoWifiScript {
		t.Fatalf("no script: %v", err)
	}
	scripts := map[string]string{
		"usr/bin/wifi_on.sh": "mkdir -p " + filepath.Dir(sock) + " && touch " + sock,
		// associated at once, an address only after udhcpc
		"usr/bin/wpa_cli": "echo wpa_state=COMPLETED; [ -e " + lease + " ] && echo ip_address=192.0.2.5; true",
		"usr/bin/udhcpc":  "touch " + lease,
	}
	for p, body := range scripts {
		full := filepath.Join(root, p)
		_ = os.MkdirAll(filepath.Dir(full), 0o755)
		if err := os.WriteFile(full, []byte("#!/bin/sh\n"+body+"\n"), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if on, ok := wifiState(); on || ok {
		t.Fatal("off before the script")
	}
	if err := wifiOn(5 * time.Second); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(lease); err != nil {
		t.Fatal("udhcpc was not asked for an address")
	}
	if on, ok := wifiState(); !on || !ok {
		t.Fatal("connected afterwards")
	}
}

func TestBatteryGuard(t *testing.T) {
	s := defaultSettings() // 20 % to start, 10 % floor while syncing
	for _, c := range []struct {
		pct             int
		charging, start bool
		want            bool
	}{
		{19, false, true, true},
		{20, false, true, false},
		{5, true, true, false},   // charging: never blocks
		{-1, false, true, false}, // unknown level
		{15, false, false, false},
		{9, false, false, true},
		{9, true, false, false},
	} {
		if got := batteryBlocks(s, c.pct, c.charging, c.start); got != c.want {
			t.Errorf("%+v: %v", c, got)
		}
	}
}

func TestHistoryKeepsTheLastTen(t *testing.T) {
	var h []HistoryEntry
	for i := 0; i < 13; i++ {
		h = addHistory(h, HistoryEntry{At: string(rune('a' + i))})
	}
	if len(h) != historyLen || h[0].At != "m" || h[9].At != "d" {
		t.Fatalf("%v", h)
	}
}

func TestFontsHaveLatinCyrillicAndSizes(t *testing.T) {
	f, err := loadFonts()
	if err != nil {
		t.Fatal(err)
	}
	for _, sz := range []struct{ size, weight int }{{13, wRegular}, {15, wRegular}, {17, wRegular}, {20, wSemi}, {24, wBold}, {32, wBold}} {
		fc := f.face(sz.size, sz.weight)
		if fc.size != sz.size || fc.weight != sz.weight {
			t.Fatalf("no face %v", sz)
		}
		for _, r := range "AzЖїЄҐ…·−" {
			if fc.find(r) == nil {
				t.Errorf("%dpx lacks %q", sz.size, r)
			}
		}
	}
}

func TestCountPlural(t *testing.T) {
	if got := Count(1, "heart", "hearts"); got != "1 heart" {
		t.Fatal(got)
	}
	if got := Count(2, "heart", "hearts"); got != "2 hearts" {
		t.Fatal(got)
	}
	if got := summary(Result{Sent: map[string]int{"play": 1, "favourite": 2}}); !strings.Contains(got, "1 play, 2 hearts, 0 positions") {
		t.Fatal(got)
	}
}

func TestEndGridSingular(t *testing.T) {
	g := strings.Join(endGrid(Result{Fetched: 1, Sent: map[string]int{"play": 1, "favourite": 1, "position": 1}}, &downPlan{}, 1), "|")
	for _, want := range []string{"+1 track|", "1 play sent", "1 heart sent", "1 playlist updated", "1 position"} {
		if !strings.Contains(g, want) {
			t.Fatalf("%q missing in %q", want, g)
		}
	}
	if strings.Contains(g, "1 playlists") || strings.Contains(g, "1 positions") {
		t.Fatal(g)
	}
}

func TestLongButtonListScrollsWithSelection(t *testing.T) {
	f, err := loadFonts()
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("SYNCMUSIC_FB", filepath.Join(t.TempDir(), "fb.rgb565"))
	t.Setenv("COMPAS_SCREEN_WIDTH", "480")
	t.Setenv("COMPAS_SCREEN_HEIGHT", "800")
	s, err := openScreen(f)
	if err != nil {
		t.Fatal(err)
	}
	defer s.file.Close()
	var bs []button
	for i := range 30 {
		bs = append(bs, button{label: fmt.Sprintf("Template %d", i+1), act: nil})
	}
	p := page{title: "Templates", lines: []line{{text: "hint", col: colDim}}, buttons: bs}
	for sel := range bs {
		rs := s.drawPage(p, sel, "")
		r := rs[sel]
		if r.w == 0 || r.y < 0 || r.y+r.h > 800 {
			t.Fatalf("selected row %d not on the screen: %+v", sel, r)
		}
		for i, o := range rs {
			if o.w > 0 && i != sel && o.y+o.h > 800 {
				t.Fatalf("row %d off screen", i)
			}
		}
	}
}

func TestPlayHoldDoesNotExit(t *testing.T) {
	t0 := time.Unix(1000, 0)
	k := combo{hold: 3 * time.Second}
	k.feed(keyPlayPause, true, t0)
	if k.fired(t0.Add(10 * time.Second)) {
		t.Fatal("holding play alone must not close the app")
	}
}

func TestPlaySelectsOnPress(t *testing.T) {
	t0 := time.Unix(1000, 0)
	hits := 0
	a := &App{k: combo{hold: 3 * time.Second}, mode: screenOn, bl: &backlight{}, s: &screen{}}
	a.build = func() page { return page{buttons: []button{{label: "x", act: func() { hits++ }}}} }
	a.handle(inEv{code: keyPlayPause, down: true}, t0)
	if hits != 1 {
		t.Fatal("play acts on press")
	}
	a.handle(inEv{code: keyPlayPause}, t0.Add(300*time.Millisecond))
	if hits != 1 {
		t.Fatal("the release does nothing")
	}
	a.mode = screenOff
	a.handle(inEv{code: keyPlayPause, down: true}, t0.Add(10*time.Second))
	if hits != 1 || a.mode != screenOn {
		t.Fatal("a wake press only wakes")
	}
}

func TestDragScrollsAndDoesNotTap(t *testing.T) {
	hits := 0
	a := &App{mode: screenOn, bl: &backlight{}, s: &screen{off: 50}}
	a.build = func() page { return page{buttons: []button{{label: "x", act: func() { hits++ }}}} }
	a.rects = []rect{{0, 0, 480, 800}}
	a.handle(inEv{drag: true, dy: -30}, time.Now())
	if !a.s.swiped || a.s.scroll != 80 || hits != 0 {
		t.Fatalf("drag up scrolls down from the shown offset: %+v hits %d", a.s, hits)
	}
	a.handle(inEv{drag: true, dy: 200}, time.Now())
	if a.s.scroll != 0 {
		t.Fatal("scroll stops at the top")
	}
	a.handle(inEv{code: keyVolDown, down: true}, time.Now())
	if a.s.swiped {
		t.Fatal("a key brings the selection back into view")
	}
}

func touchEvents(t *testing.T, ys []int32) <-chan inEv {
	rec := 8 + 2*int(unsafe.Sizeof(uintptr(0)))
	var b []byte
	ev := func(typ, code uint16, val int32) {
		r := make([]byte, rec)
		binary.LittleEndian.PutUint16(r[rec-8:], typ)
		binary.LittleEndian.PutUint16(r[rec-6:], code)
		binary.LittleEndian.PutUint32(r[rec-4:], uint32(val))
		b = append(b, r...)
	}
	ev(evAbs, absMT, 7)
	ev(evAbs, absMX, 100)
	for _, y := range ys {
		ev(evAbs, absMY, y)
		ev(0, 0, 0)
	}
	ev(evAbs, absMT, -1)
	ev(0, 0, 0)
	p := filepath.Join(t.TempDir(), "ev")
	os.WriteFile(p, b, 0o644)
	t.Setenv("SYNCMUSIC_INPUT", p)
	return inputs(480, 800)
}

func TestSwipeSendsDragsNoTap(t *testing.T) {
	ch := touchEvents(t, []int32{400, 405, 380, 300})
	var got []inEv
	for {
		select {
		case e := <-ch:
			got = append(got, e)
			continue
		case <-time.After(500 * time.Millisecond):
		}
		break
	}
	sum := 0
	for _, e := range got {
		if e.tap {
			t.Fatal("a swipe must not tap")
		}
		sum += e.dy
	}
	if len(got) != 2 || sum != -100 {
		t.Fatalf("got %+v", got)
	}
}

func TestSmallMoveIsTap(t *testing.T) {
	ch := touchEvents(t, []int32{400, 408})
	select {
	case e := <-ch:
		if !e.tap || e.y != 408 {
			t.Fatalf("got %+v", e)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("no tap")
	}
}

func TestFlushWritesOnlyChangedRows(t *testing.T) {
	s := &screen{w: 4, h: 3, pix: make([]uint16, 12), out: make([]byte, 24), stride: 8}
	s.flush()
	for i := range s.out {
		s.out[i] = 0xAA // mark
	}
	s.pix[5] = 0x1234 // row 1 only
	s.flush()
	if s.out[0] != 0xAA || s.out[16] != 0xAA || binary.LittleEndian.Uint16(s.out[10:]) != 0x1234 || s.out[8] != 0 {
		t.Fatalf("out %x", s.out)
	}
}

func TestTouchTapFromTrackingID(t *testing.T) {
	// a multi-touch screen that sends no BTN_TOUCH: the finger lifting is ABS_MT_TRACKING_ID -1
	rec := 8 + 2*int(unsafe.Sizeof(uintptr(0)))
	var b []byte
	ev := func(typ, code uint16, val int32) {
		r := make([]byte, rec)
		binary.LittleEndian.PutUint16(r[rec-8:], typ)
		binary.LittleEndian.PutUint16(r[rec-6:], code)
		binary.LittleEndian.PutUint32(r[rec-4:], uint32(val))
		b = append(b, r...)
	}
	ev(evAbs, absMT, 7)
	ev(evAbs, absMX, 100)
	ev(evAbs, absMY, 200)
	ev(0, 0, 0)
	ev(evAbs, absMT, -1)
	ev(0, 0, 0)
	p := filepath.Join(t.TempDir(), "ev")
	os.WriteFile(p, b, 0o644)
	t.Setenv("SYNCMUSIC_INPUT", p)
	select {
	case e := <-inputs(480, 800):
		if !e.tap || e.x != 100 || e.y != 200 {
			t.Fatalf("got %+v", e)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("no tap")
	}
}

func TestPreviewConfirmLabelFitsHalfButton(t *testing.T) {
	f, err := loadFonts()
	if err != nil {
		t.Fatal(err)
	}
	// a half-width button on a 480 px screen is about 210 px wide, minus 24 px padding
	for _, k := range []string{"preview.confirm_start", "preview.start", "btn.cancel"} {
		if l := T(k); f.face(22, wBold).fit(l, 186, 0) != l {
			t.Fatalf("%q does not fit a half-width button", l)
		}
	}
}

// TestEveryMenuHasAWayOut: Home has Exit; the menus have Back (or Exit) so no screen traps the user.
func TestEveryMenuHasAWayOut(t *testing.T) {
	a := &App{c: &Client{root: t.TempDir(), st: State{Server: "http://192.0.2.10:8080/"}}, set: defaultSettings(),
		later: make(chan func(), 1)} // the Wi-Fi page's background result lands here
	a.online.Store(ptr(T("home.online")))
	out := func(p page) bool {
		for _, b := range p.buttons {
			if b.kind == btnBack || b.label == T("btn.exit") || b.label == T("btn.cancel") {
				return true
			}
		}
		return false
	}
	home := a.home()
	if home.buttons[len(home.buttons)-1].label != T("btn.exit") {
		t.Fatal("Home has no Exit button")
	}
	for name, p := range map[string]page{"settings": a.settings(), "about": a.about(), "history": a.history(),
		"wifi": a.wifiPage(false, a.home)()} {
		if !out(p) {
			t.Errorf("%s has no way back or out", name)
		}
	}
	w := a.wifiPage(false, a.home)
	w().buttons[0].act() // turning Wi-Fi on: the busy page keeps an Exit
	if !out(w()) {
		t.Error("the Wi-Fi busy page has no Exit")
	}
}
