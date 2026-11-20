// SPDX-License-Identifier: GPL-3.0-or-later
// The app's screens: one event loop, pages built from text lines and a list of buttons.
// Volume up/down (or previous/next) move the selection, play selects; a tap on a button selects it too, a vertical
// swipe scrolls a long page.
// Holding next/previous + volume up (or volume up + volume down) for 3 s closes the app at any time (a running sync stops at a safe point).
package main

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"syscall"
	"time"
)

// line is one block of a page, drawn top to bottom: a text line (wrapped), a row of stat tiles, a progress bar or
// a phase bar. Consecutive blocks with the same box are drawn inside one rounded card (boxWarn: an amber one).
type line struct {
	text  string
	col   uint16
	box   uint8
	size  int      // 0/1 text, 2 heading, 3 a code to compare (heading font, twice the size)
	stats []stat   // columns with a small grey label above the value instead of text
	bar   []int64  // a progress bar: done, total (and a grey part after it: done, total, other)
	steps []int    // a phase bar: current (1-based), count
	right string   // a value drawn right-aligned on the same row
	rcol  uint16   // its colour (default: the text's)
	grid  []string // short texts in two columns
	names []string // under a phase bar: the phases
	check *bool    // a check box with the text
	dot   uint16   // a coloured dot before the text
}

type stat struct{ label, value string }

const (
	boxNone uint8 = iota
	boxCard
	boxWarn
	boxCard2 // a second card right after a boxCard one
	boxNote  // a teal note
)

type button struct {
	label string
	act   func()
	kind  uint8  // btnNormal, btnPrimary (accent), btnDanger
	group string // a settings row: rows of one group are drawn as one card under the group's name
	half  bool   // two half buttons next to each other side by side
}

const (
	btnNormal uint8 = iota
	btnPrimary
	btnDanger
	btnBack // the back key in the header
)

type page struct {
	title       string
	lines       []line
	done, total int // progress bar when total > 0
	buttons     []button
	grid        int    // the first `grid` buttons are keys of an on-screen keyboard, gridCols per row
	cols        int    // keys per row instead of gridCols (3 = the pairing-code number pad)
	field       string // what is typed so far (with a grid)
	home        bool   // the home page header: the logo and the status
	sub         string // a grey line under the title
	foot        string // a small hint at the bottom
	icon        uint8  // drawn before the title (or above it on a centred page)
	centered    bool   // icon, title, text and buttons in the middle of the screen
	showStatus  bool   // the status (battery) at the right of the title
}

const gridCols = 6

// the name a new pairing gets on the server (the user can rename it there)
const playerName = "HiBy R1 - Compás"

type rect struct{ x, y, w, h int }

func (r rect) has(x, y int) bool { return x >= r.x && x < r.x+r.w && y >= r.y && y < r.y+r.h }

type syncDone struct {
	res Result
	d   *downPlan
	err error
}

type App struct {
	s               *screen
	c               *Client
	set             Settings
	bl              *backlight
	in              <-chan inEv
	build           func() page // the current page, rebuilt on every draw
	sel             int
	rects           []rect
	shown           string // last logged page, to log every page once
	idle            time.Time
	mode            int
	k               combo
	syncing         bool
	stop            atomic.Bool
	syncPage        func() page // the running sync's page (back to it after the preview)
	lowBattery      atomic.Bool // the sync stopped because the battery ran low
	closing         bool        // forced exit: quit as soon as the sync stopped
	quit            bool
	online          atomic.Pointer[string]
	update          atomic.Pointer[appUpdate] // a newer app release the server offers, nil = none
	powerAt         time.Time                 // when the Power key went down (zero = not held)
	offFlag         atomic.Bool               // the screen is off (read by the Wi-Fi log goroutine)
	newerThanServer string                    // the server's older app version when this app is newer ("" = not)
	prog            chan [3]any               // done, total, what
	done            chan syncDone
	later           chan func() // work finished in a goroutine, applied on the loop
	gen             int         // bumped when the user leaves a pairing page: late results are dropped
	pdone           int
	ptotal          int
	pwhat           string
	slow            int          // slow frames logged
	hist            frameHist    // all drawn frames, logged at exit
	frame           string       // what the last drawn frame showed (frameKey)
	exitFn          func() error // what happens after the loop (restart, power off); nil = just exit
}

func (a *App) go_(build func() page) {
	a.build, a.sel = build, 0
	if a.s != nil {
		a.s.swiped = false
	}
}

func (a *App) render() {
	if a.mode == screenOff {
		return
	}
	p := a.build()
	if a.sel >= len(p.buttons) {
		a.sel = max(0, len(p.buttons)-1)
	}
	status := a.statusLine()
	ck := frameKey(p, a.sel, "", nil) // the page content; the frame adds the status and the scroll position
	fk := ck + "|" + status
	if a.s != nil {
		fk += fmt.Sprintf("|S%v %d", a.s.swiped, a.s.scroll)
		a.s.pageKey = ck
	}
	if fk != a.frame || a.rects == nil {
		a.frame = fk
	} else {
		return // nothing on the screen changed (idle ticks): drawing costs the player a lot
	}
	t0 := time.Now()
	a.rects = a.s.drawPage(p, a.sel, status)
	d := time.Since(t0)
	a.hist.add(d)
	if d > 50*time.Millisecond && a.slow < 40 { // slow frames (over 50 ms) go to startup.log
		a.slow++
		step("slow frame: %s took %d ms", p.title, d.Milliseconds())
	}
	key := p.title
	for _, t := range flat(p.lines) {
		key += " | " + t
	}
	if key != a.shown {
		if a.shown == "" {
			step("first screen drawn: %s", p.title)
		}
		a.shown = key
		fmt.Fprintf(os.Stderr, "ui: %s | %d/%d\n", key, p.done, p.total)
	}
}

// frameKey describes everything a drawn frame depends on: the same key means the same pixels.
func frameKey(p page, sel int, status string, s *screen) string {
	var b strings.Builder
	fmt.Fprintf(&b, "%q|%d|%d|%d|%d|%q|%v|%q|%q|%d|%v|%v|%q|", p.title, p.done, p.total, p.grid, p.cols, p.field, p.home,
		p.sub, p.foot, p.icon, p.centered, p.showStatus, status)
	for _, l := range p.lines {
		chk := -1
		if l.check != nil && *l.check {
			chk = 1
		} else if l.check != nil {
			chk = 0
		}
		fmt.Fprintf(&b, "L%q %d %d %d %v %v %v %q %d %q %q %d %d;", l.text, l.col, l.box, l.size, l.stats, l.bar, l.steps,
			l.right, l.rcol, l.grid, l.names, chk, l.dot)
	}
	for _, x := range p.buttons {
		fmt.Fprintf(&b, "B%q %d %q %v;", x.label, x.kind, x.group, x.half)
	}
	fmt.Fprintf(&b, "S%d", sel)
	if s != nil {
		fmt.Fprintf(&b, " %v %d", s.swiped, s.scroll)
	}
	return b.String()
}

// drainInput handles the input events already queued without blocking (at most 64, so a flood cannot starve
// drawing).
func (a *App) drainInput() {
	for range 64 {
		select {
		case e := <-a.in:
			a.handle(e, time.Now())
		default:
			return
		}
	}
}

// frameHist counts drawn frames by duration.
type frameHist [6]int

var histEdges = [...]time.Duration{10, 20, 30, 50, 100} // ms; the last bucket is everything slower

func (h *frameHist) add(d time.Duration) {
	i := 0
	for i < len(histEdges) && d >= histEdges[i]*time.Millisecond {
		i++
	}
	h[i]++
}

func (h *frameHist) String() string {
	var b strings.Builder
	lo := 0
	for i, n := range h {
		if i < len(histEdges) {
			fmt.Fprintf(&b, "%d-%dms:%d ", lo, histEdges[i], n)
			lo = int(histEdges[i])
		} else {
			fmt.Fprintf(&b, ">=%dms:%d", lo, n)
		}
	}
	return b.String()
}

func (a *App) statusLine() string {
	parts := []string{}
	if _, ok := wifiState(); ok && !a.syncing {
		parts = append(parts, "Wi-Fi ●")
	}
	if pct, charging := battery(); pct >= 0 {
		v := fmt.Sprintf("%d%%", pct)
		if charging {
			v = "⚡ " + v
		}
		parts = append(parts, v)
	}
	st := strings.Join(parts, "   ")
	if a.syncing && a.set.KeepAwake {
		st += " · " + T("sync.stays_on")
	}
	return st
}

// powerKey: a short press (released within a second) switches the screen off, or on again; a long press does
// nothing here, so the player's own long-press power off keeps working. A sync goes on with the screen off.
func (a *App) powerKey(down bool, now time.Time) {
	if down {
		a.powerAt = now
		return
	}
	short := !a.powerAt.IsZero() && now.Sub(a.powerAt) < time.Second
	a.powerAt = time.Time{}
	if !short {
		return
	}
	a.idle = now
	if a.mode == screenOff {
		a.setScreen(screenOn)
	} else {
		a.setScreen(screenOff)
	}
}

// setScreen switches the backlight; switching off starts the Wi-Fi log while the screen stays off.
func (a *App) setScreen(m int) {
	was := a.mode
	a.mode = m
	if m == screenOn {
		a.frame = ""
	}
	a.bl.set(m, a.set.DimPercent)
	if m == screenOff && was != screenOff {
		step("screen off: %s", wifiDiag())
		a.offFlag.Store(true)
		go watchWhileOff(a.c, a.offFlag.Load)
	} else if m != screenOff && was == screenOff {
		a.offFlag.Store(false)
		step("screen on: %s", wifiDiag())
	}
}

func (a *App) handle(e inEv, now time.Time) {
	if !e.tap {
		a.k.feed(e.code, e.down, now)
	}
	if !e.tap && !e.drag && e.code == keyPower {
		a.powerKey(e.down, now)
		return
	}
	if !e.tap && !e.drag && !e.down {
		return // keys act on press
	}
	a.idle = now
	if a.mode != screenOn { // the first input only wakes the screen
		a.setScreen(screenOn)
		return
	}
	if e.drag {
		if !a.s.swiped {
			a.s.swiped, a.s.scroll = true, a.s.off
		}
		a.s.scroll = max(0, a.s.scroll-e.dy) // drawPage clamps the bottom end
		return
	}
	if a.s != nil {
		a.s.swiped = false // keys and taps: the page follows the selection again
	}
	p := a.build()
	if e.tap {
		for i, r := range a.rects {
			if r.has(e.x, e.y) && i < len(p.buttons) {
				a.sel = i
				p.buttons[i].act()
				return
			}
		}
		return
	}
	switch nav(e.code) {
	case navUp:
		if a.sel > 0 {
			a.sel--
		}
	case navDown:
		if a.sel < len(p.buttons)-1 {
			a.sel++
		}
	case navSelect:
		if a.sel < len(p.buttons) {
			p.buttons[a.sel].act()
		}
	}
}

func (a *App) forceExit() {
	if a.closing {
		return
	}
	a.closing = true
	a.exitFn = nil
	fmt.Fprintln(os.Stderr, "forced exit")
	if !a.syncing {
		a.quit = true
		return
	}
	a.stop.Store(true)
	a.go_(func() page {
		return page{title: T("exit.forced"), lines: []line{{text: T("sync.stopping"), col: colDim}}}
	})
}

func (a *App) loop() error {
	tick := time.NewTicker(time.Second)
	defer tick.Stop()
	for !a.quit {
		a.render()
		select {
		case e := <-a.in:
			a.handle(e, time.Now())
			a.drainInput() // a swipe sends a move per touch report: draw once for all that are waiting
		case p := <-a.prog:
			a.pdone, a.ptotal, a.pwhat = p[0].(int), p[1].(int), p[2].(string)
		case d := <-a.done:
			a.syncing = false
			if a.closing {
				a.quit = true
			} else {
				a.finish(d)
			}
		case f := <-a.later:
			f()
		case <-tick.C:
		}
		now := time.Now()
		if a.k.fired(now) {
			a.forceExit()
		}
		if m := screenMode(a.set, a.mode, now.Sub(a.idle), a.syncing); m != a.mode {
			a.setScreen(m)
		}
	}
	a.offFlag.Store(false)
	a.bl.restore()
	releaseWakeLock()
	step("frame times: %s", a.hist.String())
	profStop()
	if a.exitFn != nil {
		return a.exitFn()
	}
	return nil
}

// --- pages ----------------------------------------------------------------------------------------

func (a *App) home() page {
	st := a.c.st
	online := *a.online.Load()
	oc := colDim
	if online == T("home.online") {
		oc = colTeal2
	} else if online == T("home.offline") {
		oc = colErr
	}
	name, addr := serverName(st.Server)
	ls := []line{{text: T("home.server_l"), size: sizeLabel, col: colDim, box: boxCard},
		{text: name, size: sizeHead, box: boxCard}, {text: addr, size: sizeSmall, col: colSoft, box: boxCard},
		{text: upFirst(online), size: sizeSmall, col: oc, dot: oc, box: boxCard}}
	last := T("home.last_l")
	if st.LastSync != "" {
		last += " · " + ago(st.LastSync, time.Now())
	}
	ls = append(ls, line{text: last, size: sizeLabel, col: colDim, box: boxCard2})
	switch {
	case len(st.LastGrid) > 0:
		ls = append(ls, line{grid: st.LastGrid, box: boxCard2})
	case st.LastSync != "":
		ls = append(ls, line{text: st.LastSummary, col: colSoft, box: boxCard2})
	default:
		ls = append(ls, line{text: T("home.never"), col: colSoft, box: boxCard2})
	}
	if total, free := freeSpace(a.c.root); total > 0 {
		var ours int64
		for _, n := range st.Files {
			ours += n
		}
		other := max(0, total-free-ours)
		ls = append(ls, line{text: T("home.card_l"), right: T("home.free_of", gbShort(free), gbShort(total)), size: sizeLabel, col: colDim},
			line{bar: []int64{ours, total, other}},
			line{grid: []string{"■ " + T("home.ours_l", gbShort(ours)), "■ " + T("home.other_l", gbShort(other))}, col: colSoft, size: sizeHint})
		for i := len(ls) - 3; i < len(ls); i++ {
			ls[i].box = boxCard
		}
	}
	switch on, ok := wifiState(); {
	case ok:
	case on:
		ls = append(ls, line{text: T("home.wifi_on"), col: colErr})
	default:
		ls = append(ls, line{text: T("home.wifi_off"), col: colErr})
	}
	bs := []button{{label: T("btn.sync"), act: a.startSync, kind: btnPrimary}}
	if a.newerThanServer != "" {
		ls = append(ls, line{text: T("home.newer", version, a.newerThanServer), col: colWarn})
	}
	if u := a.update.Load(); u != nil {
		ls = append(ls, line{text: T("home.update", u.Version), col: colTeal2})
		bs = append(bs, button{label: T("btn.update", u.Version), act: func() { a.installUpdate(*u) }})
	}
	return page{title: T("home.title"), home: true, lines: ls, foot: T("foot.exit"), buttons: append(bs,
		button{label: T("btn.settings"), act: func() { a.go_(a.settings) }, half: true},
		button{label: T("btn.history"), act: func() { a.go_(a.history) }, half: true},
		button{label: T("btn.exit"), act: func() { a.quit = true }},
	)}
}

// serverName: the host of a server address as its name, and the address without the scheme.
func serverName(url string) (string, string) {
	addr := strings.TrimSuffix(strings.TrimPrefix(strings.TrimPrefix(url, "http://"), "https://"), "/")
	host := addr
	if i := strings.LastIndex(host, ":"); i > 0 {
		host = host[:i]
	}
	if host == "" {
		host = "–"
	}
	return host, addr
}

func upFirst(s string) string {
	if s == "" {
		return s
	}
	r := []rune(s)
	return strings.ToUpper(string(r[0])) + string(r[1:])
}

// ago: "2 h ago" for a local time stamp "2006-01-02 15:04".
func ago(at string, now time.Time) string {
	t, err := time.ParseInLocation("2006-01-02 15:04", at, time.Local)
	if err != nil {
		return at
	}
	switch d := now.Sub(t); {
	case d < 2*time.Minute:
		return T("ago.now")
	case d < time.Hour:
		return T("ago.min", int(d.Minutes()))
	case d < 48*time.Hour:
		return T("ago.h", int(d.Hours()))
	default:
		return T("ago.d", int(d.Hours()/24))
	}
}

// gbShort: "118 GB" (whole gigabytes from 10 GB on).
func gbShort(n int64) string {
	if n >= 10e9 {
		return fmt.Sprintf("%d GB", (n+5e8)/1e9)
	}
	return gb(n)
}

// offerUpdate: right after the app reached the server. A newer app on the server is offered once per start
// (Update / Later, never installed without a press); an app newer than the server's only gets a notice.
func (a *App) offerUpdate(u appUpdate) {
	if u.ClientNewer && u.ServerVersion != "" {
		a.newerThanServer = u.ServerVersion
	}
	if !u.Available {
		return
	}
	a.update.Store(&u)
	if a.syncing || a.build().title != T("home.title") {
		return // the home page keeps the Update button; no interruption of a sync or pairing
	}
	a.go_(func() page {
		return page{title: T("update.title"), lines: []line{
			{text: T("update.offer", u.Version, version), col: colText},
			{text: T("update.offer_hint"), col: colDim},
		}, buttons: []button{
			{label: T("btn.update_now"), kind: btnPrimary, act: func() {
				a.c.AnswerUpdate(u.Version, "update")
				a.installUpdate(u)
			}},
			{label: T("btn.later"), act: func() {
				a.c.AnswerUpdate(u.Version, "later")
				a.go_(a.home)
			}},
		}}
	})
}

// installUpdate downloads and installs the app release the server offers; Compás asks to approve it on the next
// start of the app.
func (a *App) installUpdate(u appUpdate) {
	if a.syncing {
		return
	}
	a.go_(func() page {
		return page{title: T("update.title"), lines: []line{{text: T("update.installing", u.Version), col: colDim}}}
	})
	a.render()
	if err := a.c.InstallUpdate(u); err != nil {
		a.go_(a.message(T("update.title"), T("update.failed", err.Error()), true))
		return
	}
	a.update.Store(nil)
	a.go_(func() page {
		return page{title: T("update.title"), lines: []line{{text: T("update.done", u.Version), col: colText},
			{text: T("update.approve"), col: colDim}}, buttons: []button{
			{label: T("btn.exit"), act: func() { a.quit = true }},
			{label: T("btn.home"), act: func() { a.go_(a.home) }},
		}}
	})
}

func (a *App) settings() page {
	s := &a.set
	save := func() {
		_ = s.save(a.c.data)
		c, cp := a.c, *s
		go c.pushSettings(cp)
	}
	var warn []line
	if s.dimAfterOff() {
		warn = []line{{text: T("set.dim_after_off"), col: colErr}}
	}
	return page{title: T("set.title"), lines: warn, buttons: []button{
		{label: T("set.dim", seconds(s.DimAfterS)), act: func() { s.DimAfterS = next(dimChoices, s.DimAfterS); save() }, group: T("set.g_screen")},
		{label: T("set.dim_pct", s.DimPercent), act: func() { s.DimPercent = next(percentChoices, s.DimPercent); save() }, group: T("set.g_screen")},
		{label: T("set.off", seconds(s.OffAfterS)), act: func() { s.OffAfterS = next(offChoices, s.OffAfterS); save() }, group: T("set.g_screen")},
		{label: T("set.awake", yesNo(s.KeepAwake)), act: func() { s.KeepAwake = !s.KeepAwake; save() }, group: T("set.g_screen")},
		{label: T("set.on_start", yesNo(s.SyncOnStart)), act: func() { s.SyncOnStart = !s.SyncOnStart; save() }, group: T("set.g_sync")},
		{label: T("set.after", T("after."+s.AfterSync)), act: func() { s.AfterSync = next(afterChoices, s.AfterSync); save() }, group: T("set.g_sync")},
		{label: T("set.min_battery", s.MinBattery), act: func() { s.MinBattery = next(batteryChoices, s.MinBattery); save() }, group: T("set.g_sync")},
		{label: T("set.templates"), act: a.openTemplates, group: T("set.g_sync")},
		{label: T("set.name", a.shownName()), act: func() { a.go_(a.message(T("set.name_where"), T("set.name_hint"), false)) }, group: T("set.g_server")},
		{label: T("set.server", serverOnly(a.c.st.Server)), act: func() { a.go_(a.pairMenu) }, group: T("set.g_server")},
		{label: T("btn.about"), act: func() { a.go_(a.about) }, group: T("set.g_server")},
		{label: T("set.reset"), act: func() { a.go_(a.resetConfirm) }, group: T("set.g_server")},
		{label: T("btn.exit_app"), act: func() { a.quit = true }, group: T("set.g_server")},
		{label: T("btn.back"), kind: btnBack, act: func() { a.go_(a.home) }},
	}}
}

// resetConfirm asks before the app's own data is deleted.
func (a *App) resetConfirm() page {
	return page{title: T("reset.title"), lines: []line{{text: T("reset.text"), col: colText}}, buttons: []button{
		{label: T("reset.yes"), kind: btnDanger, act: a.resetApp},
		{label: T("btn.back"), kind: btnBack, act: func() { a.go_(a.settings) }},
	}}
}

// resetApp deletes the app's data (pairing, settings, history) and starts over at pairing; the app itself, the
// music and the card's manifest stay.
func (a *App) resetApp() {
	if a.syncing {
		return
	}
	if err := a.c.Reset(); err != nil {
		a.go_(a.message(T("reset.failed"), err.Error(), true))
		return
	}
	a.set = defaultSettings()
	a.online.Store(ptr(T("home.offline")))
	a.go_(func() page {
		return page{title: T("reset.done"), lines: []line{{text: T("reset.done_text"), col: colText}}, buttons: []button{
			{label: T("pair.connect"), act: func() { a.go_(a.pairMenu) }, kind: btnPrimary},
			{label: T("btn.exit"), act: func() { a.quit = true }}}}
	})
}

// shownName: this player's name on the server (an install paired before names were kept: the default).
func (a *App) shownName() string {
	if a.c.st.Name != "" {
		return a.c.st.Name
	}
	return playerName
}

func serverOnly(url string) string {
	n, _ := serverName(url)
	return n
}

func (a *App) about() page {
	return page{title: T("about.title"), lines: []line{
		{text: T("about.version", version), col: colText},
		{text: T("home.server", a.c.st.Server), col: colText},
		{text: T("about.device", a.c.st.DeviceID), col: colText},
		{text: T("about.log"), col: colDim},
		{text: T("about.exit"), col: colDim},
	}, buttons: []button{{label: T("btn.history"), act: func() { a.go_(a.history) }}, {label: T("btn.diag"), act: a.sendDiagnostics},
		{label: T("btn.back"), kind: btnBack, act: func() { a.go_(a.settings) }}}}
}

func (a *App) history() page {
	ls := []line{}
	for i, h := range a.c.st.History {
		box := boxCard
		if i%2 == 1 {
			box = boxCard2 // its own card
		}
		ls = append(ls, line{text: h.At, size: sizeLabel, col: colDim, box: box}, line{text: h.Title, size: sizeRowSemi, box: box},
			line{text: h.Summary, size: sizeSmall, col: colSoft, box: box})
	}
	if len(ls) == 0 {
		ls = []line{{text: T("history.none"), col: colSoft}}
	}
	return page{title: T("history.title"), lines: ls, buttons: []button{{label: T("btn.back"), kind: btnBack, act: func() { a.go_(a.home) }}}}
}

func yesNo(b bool) string {
	if b {
		return T("yes")
	}
	return T("no")
}

func (a *App) message(title, text string, err bool) func() page {
	icon := iconCheck
	if err {
		icon = iconError
	}
	return func() page {
		return page{title: title, icon: icon, lines: []line{{text: text, col: colSoft}}, buttons: []button{
			{label: T("btn.home"), act: func() { a.go_(a.home) }, half: true},
			{label: T("btn.exit"), act: func() { a.quit = true }, half: true},
		}}
	}
}

func (a *App) startSync() { a.runSync(false) }

// runSync: auto = started by "sync when the app opens" (no preview unless the server wants a confirmation).
func (a *App) runSync(auto bool) {
	if pct, charging := battery(); batteryBlocks(a.set, pct, charging, true) {
		a.go_(a.message(T("bat.title"), T("bat.too_low", pct, a.set.MinBattery), true))
		return
	}
	a.lowBattery.Store(false)
	a.syncing, a.pdone, a.ptotal, a.pwhat = true, 0, 0, T("sync.connecting")
	a.stop.Store(false)
	a.syncPage = func() page {
		p := a.c.pr.snapshot()
		ls := syncLines(p, time.Now())
		if p.Phase == "" {
			ls = []line{{text: a.pwhat, size: sizeHead, box: boxCard}}
		}
		btn := []button{{label: T("btn.stop"), act: func() { a.stop.Store(true) }, kind: btnDanger}}
		if a.stop.Load() {
			ls, btn = append([]line{{text: T("sync.stopping"), col: colTeal2}}, ls...), nil
		}
		return page{title: T("sync.title_short"), showStatus: true, lines: ls, buttons: btn}
	}
	a.go_(a.syncPage)
	c := a.c
	c.preview = func(p Preview) bool {
		if !p.Confirm && (auto || p.FetchCount+p.DeleteCount == 0) {
			return true // started by itself, or nothing to show
		}
		ans := make(chan bool, 1)
		a.later <- func() { a.go_(a.previewPage(p, ans)) }
		for {
			select {
			case ok := <-ans:
				return ok
			case <-time.After(200 * time.Millisecond):
				if a.stop.Load() {
					return false
				}
			}
		}
	}
	lastCheck := time.Now()
	c.cancel = func() bool {
		if a.stop.Load() {
			return true
		}
		if time.Since(lastCheck) > 20*time.Second { // the battery is read now and then, between files
			lastCheck = time.Now()
			if pct, charging := battery(); batteryBlocks(a.set, pct, charging, false) {
				a.lowBattery.Store(true)
				a.stop.Store(true)
				fmt.Fprintln(os.Stderr, "battery low: stopping the sync")
			}
		}
		return a.stop.Load()
	}
	c.progress = func(done, total int, what string) {
		select {
		case a.prog <- [3]any{done, total, what}:
		default: // the screen catches up with the next one
		}
	}
	go func() {
		res, err := c.Sync()
		var d *downPlan
		if err == nil {
			var derr error
			c.pr.update(func(p *Progress) { p.Phase = phasePositions })
			if d, derr = c.down(res.State, res.SentHearts, res.SentPositions); derr != nil {
				fmt.Fprintln(os.Stderr, "tagcache not read:", derr)
			} else {
				c.pr.update(func(p *Progress) { p.HeartsDown, p.PositionsDown = len(d.patches), len(d.positions) })
			}
		}
		a.done <- syncDone{res, d, err}
	}()
}

// finish shows the result and does what the settings say after a sync that changed something.
func (a *App) finish(sd syncDone) {
	res, d, err := sd.res, sd.d, sd.err
	var pe *ProtoError
	if errors.As(err, &pe) && pe.Code == "no_templates" {
		a.go_(a.message(T("nothing.title"), T("nothing.text"), false))
		return
	}
	if errors.As(err, &pe) && pe.Code == "registration_pending" { // not set up on the server yet: wait for it
		a.waitRegistration(a.home)
		return
	}
	if errors.Is(err, errDeclined) {
		a.go_(a.home)
		return
	}
	if err != nil {
		a.c.st.History = addHistory(a.c.st.History, HistoryEntry{time.Now().Format("2006-01-02 15:04"), T("end.failed"), err.Error()})
		_ = a.c.save()
		a.go_(a.message(T("end.failed"), err.Error(), true))
		return
	}
	if d == nil {
		d = &downPlan{}
	}
	title := T("end.finished")
	if res.Cancelled {
		title = T("end.stopped")
		if a.lowBattery.Load() {
			title = T("end.battery")
		}
	}
	msg := summary(res)
	notes := ""
	if len(d.patches) > 0 {
		notes += fmt.Sprintf(" To the player: %s.", Count(len(d.patches), "heart or play count", "hearts and play counts"))
	}
	if len(d.positions) > 0 {
		notes += fmt.Sprintf(" To the player: %s.", Count(len(d.positions), "position", "positions"))
	}
	if res.Generation > 0 {
		notes += " The player's library was rebuilt by the server."
	}
	if pr := a.c.pr.snapshot(); pr.Bytes > 0 && !pr.Started.IsZero() {
		if el := time.Since(pr.Started).Seconds(); el >= 1 {
			notes += " " + T("end.average", mb(pr.Bytes), mb(int64(float64(pr.Bytes)/el)))
		}
	}
	msg += notes
	// the result page: the counts in a card, how long it took under the title
	grid := endGrid(res, d, a.c.pr.snapshot().Playlists)
	result := []line{{grid: grid, box: boxCard}}
	if res.Failed > 0 {
		result = append(result, line{text: T("end.not_done", res.Failed), col: colErr})
	}
	sub := ""
	if pr := a.c.pr.snapshot(); !pr.Started.IsZero() {
		sub = duration(time.Since(pr.Started))
		if pr.Bytes > 0 {
			sub += " · " + mbShort(pr.Bytes)
		}
	}
	a.c.st.LastSync, a.c.st.LastSummary, a.c.st.LastGrid = time.Now().Format("2006-01-02 15:04"), msg, grid
	a.c.st.History = addHistory(a.c.st.History, HistoryEntry{a.c.st.LastSync, title, msg})
	_ = a.c.save()
	changed := res.Fetched+res.Deleted+len(d.patches)+len(d.positions)+res.Generation > 0
	if !changed {
		_ = a.c.apply(d) // nothing to write: the library's state is the new baseline
		a.go_(func() page {
			return page{title: title, icon: iconCheck, sub: sub, lines: result, buttons: []button{
				{label: T("btn.home"), act: func() { a.go_(a.home) }, kind: btnPrimary},
				{label: T("btn.exit"), act: func() { a.quit = true }}}}
		})
		return
	}
	// The player shows new and removed files and the library's hearts only after a restart (it reloads its
	// database and rescans the card on boot). The hearts are written into the database right before it.
	restartWith := func(cmd string) func() {
		return func() {
			if err := a.c.apply(d); err != nil {
				fmt.Fprintln(os.Stderr, "hearts not written:", err) // the next run tries again
			}
			label := T("end.restarting")
			if cmd == afterPowerOff {
				label = T("end.powering_off")
			}
			a.go_(func() page { return page{title: label, lines: []line{{text: msg, col: colText}}} })
			a.render()
			a.exitFn = func() error { return restart(a.c.root, cmd) }
			a.quit = true
		}
	}
	ask := func() page {
		return page{title: title, icon: iconCheck, sub: sub, lines: append(result[:len(result):len(result)], line{text: T("end.need_restart"), col: colNoteTxt, box: boxNote}), buttons: []button{
			{label: T("end.btn_restart"), act: restartWith(afterReboot), kind: btnPrimary},
			{label: T("btn.poweroff"), act: restartWith(afterPowerOff), half: true},
			{label: T("btn.exit"), act: func() { a.quit = true }, half: true},
		}}
	}
	act := a.set.AfterSync
	if act == afterAsk {
		a.go_(ask)
		return
	}
	if act == afterNothing {
		a.go_(func() page {
			p := ask()
			p.buttons = []button{{label: T("btn.home"), act: func() { a.go_(a.home) }, kind: btnPrimary}, {label: T("btn.exit"), act: func() { a.quit = true }}}
			return p
		})
		return
	}
	deadline := time.Now().Add(5 * time.Second)
	key := "end.restart_in"
	if act == afterPowerOff {
		key = "end.poweroff_in"
	}
	a.go_(func() page {
		left := int(time.Until(deadline).Seconds() + 0.99)
		if left <= 0 {
			restartWith(act)()
			return a.build()
		}
		p := ask()
		p.foot = T(key, left)
		p.buttons = append(p.buttons[:1:1], button{label: T("btn.poweroff"), act: restartWith(afterPowerOff), half: true},
			button{label: T("btn.cancel"), act: func() { a.go_(ask) }, half: true})
		if act == afterPowerOff {
			p.buttons[0], p.buttons[1] = button{label: T("btn.poweroff"), act: restartWith(afterPowerOff), kind: btnPrimary},
				button{label: T("end.btn_restart"), act: restartWith(afterReboot), half: true}
		}
		return p
	})
}

// endGrid: what a sync did, as short counts in two columns.
func endGrid(r Result, d *downPlan, lists int) []string {
	g := []string{T("end.g_tracks", Count(r.Fetched, "track", "tracks")), T("end.g_removed", r.Deleted),
		T("end.g_plays", Count(r.Sent["play"], "play", "plays")), T("end.g_hearts", Count(r.Sent["favourite"], "heart", "hearts")),
		T("end.g_lists", Count(lists, "playlist", "playlists")),
		T("end.g_pos", Count(r.Sent["position"]+len(d.positions), "position", "positions"))}
	if r.Generation > 0 {
		g = append(g, T("end.g_rebuilt"))
	}
	if r.Failed > 0 {
		g = append(g, T("end.g_failed", r.Failed))
	}
	return g
}

// restart flushes the card and restarts or powers off the device (`SYNCMUSIC_RESTART` = another command,
// for tests; it gets $SYNCMUSIC_HOW = reboot | poweroff). The player's process exiting would reboot it too, but that is not ours to do.
func restart(root, how string) error {
	syscall.Sync()
	listDB(root, "before the restart")
	if cmd := os.Getenv("SYNCMUSIC_RESTART"); cmd != "" {
		sh := exec.Command("/bin/sh", "-c", cmd)
		sh.Env = append(os.Environ(), "SYNCMUSIC_HOW="+how)
		return sh.Run()
	}
	if how == afterPowerOff {
		if err := exec.Command("poweroff").Run(); err == nil {
			return nil
		}
		return syscall.Reboot(syscall.LINUX_REBOOT_CMD_POWER_OFF)
	}
	if err := exec.Command("reboot").Run(); err == nil {
		return nil
	}
	return syscall.Reboot(syscall.LINUX_REBOOT_CMD_RESTART)
}

// --- start ----------------------------------------------------------------------------------------

func runApp() error {
	f, err := loadFonts()
	if err != nil {
		return err
	}
	step("font loaded")
	s, err := openScreen(f)
	if err != nil {
		step("screen not opened: %v", err)
		return err
	}
	step("screen %dx%d, line %d bytes", s.w, s.h, s.stride)
	a := &App{s: s, bl: openBacklight(), in: inputs(s.w, s.h), idle: time.Now(),
		k: combo{hold: 3 * time.Second}, prog: make(chan [3]any, 4), done: make(chan syncDone, 1),
		later: make(chan func(), 4),
	}
	step("backlight and input started")
	holdWakeLock()
	a.online.Store(ptr(T("home.checking")))
	c, err := open()
	if err != nil {
		a.c = &Client{}
		a.go_(a.message(T("err.start"), err.Error(), true))
		return a.loop()
	}
	a.c, a.set = c, loadSettings(c.data)
	startProfile(c.data)
	a.go_(a.home)
	pairFile := filepath.Join(c.data, "pair.txt")
	if b, err := os.ReadFile(pairFile); err == nil {
		fields := strings.Fields(string(b))
		switch {
		case len(fields) < 2:
			a.go_(a.message(T("pair.title"), T("pair.bad_file"), true))
		default:
			a.go_(func() page {
				return page{title: T("pair.title"), lines: []line{{text: T("pair.with", fields[0]), col: colText}}}
			})
			a.render()
			if err := c.Pair(fields[0], fields[1], playerName); err != nil {
				a.go_(a.message(T("pair.failed"), err.Error(), true))
			} else {
				_ = os.Remove(pairFile)
				a.set = loadSettings(c.data)
				a.go_(a.home)
			}
		}
	}
	if t := a.build().title; c.st.Token == "" && t != T("pair.title") && t != T("pair.failed") {
		a.go_(a.pairMenu)
	}
	step("state read, checking Wi-Fi")
	if on, ok := wifiState(); !ok {
		after := a.build
		a.go_(a.wifiPage(on, after))
	}
	if c.st.Token != "" && a.set.SyncOnStart && a.build().title == T("home.title") {
		a.runSync(true)
	}
	if c.st.Token != "" {
		probe := &Client{st: State{Server: c.st.Server}, http: newHTTPClient(5 * time.Second)}
		go func() { // is the server there? (shown on the home page)
			if err := probe.call("GET", "hello", nil, nil); err != nil {
				a.online.Store(ptr(T("home.offline")))
				return
			}
			a.online.Store(ptr(T("home.online")))
			if u, err := c.CheckUpdate(); err == nil {
				a.later <- func() { a.offerUpdate(u) }
			}
		}()
	}
	if c.st.Token != "" {
		go a.listen()
	}
	return a.loop()
}

// listen: while the app is open, a long poll on the server's status; "Sync now" pressed on the server starts the
// sync here at once (unless one runs or the user is pairing).
func (a *App) listen() {
	lp := &Client{st: State{Server: a.c.st.Server, Token: a.c.st.Token}, http: newHTTPClient(40 * time.Second)}
	for {
		var st struct {
			Requested bool   `json:"requested"`
			Name      string `json:"name"`
		}
		if err := lp.call("GET", "status?wait=25", nil, &st); err != nil {
			time.Sleep(15 * time.Second) // offline or the server restarts: try again later
			continue
		}
		if st.Name != "" {
			a.later <- func() { // renamed on the server: the server's name wins
				if a.c.st.Name != st.Name {
					a.c.st.Name = st.Name
					_ = a.c.save()
				}
			}
		}
		if st.Requested {
			started := make(chan bool, 1)
			a.later <- func() {
				ok := !a.syncing && a.build().title == T("home.title")
				if ok {
					a.runSync(true)
				}
				started <- ok
			}
			if !<-started {
				time.Sleep(5 * time.Second) // busy now: the request waits on the server
			}
		}
		time.Sleep(time.Second)
	}
}

// screenDemo draws one page and exits (test hook: `screen <title> <status> <detail>`).
func screenDemo(args []string) error {
	f, err := loadFonts()
	if err != nil {
		return err
	}
	s, err := openScreen(f)
	if err != nil {
		return err
	}
	s.drawPage(page{title: args[0], lines: []line{{text: args[1], col: colText}, {text: args[2], col: colDim}}, done: 2, total: 5,
		buttons: []button{{label: T("btn.stop"), act: nil}}}, 0, "80%")
	return nil
}

func ptr[V any](v V) *V { return &v }

// --- pairing --------------------------------------------------------------------------------------

// pairMenu: find a server on the network or type its address; for a paired app also forget the server.
func (a *App) pairMenu() page {
	paired := a.c.st.Token != ""
	p := page{title: T("pair.none"), lines: []line{{text: T("pair.intro"), col: colText}}, buttons: []button{
		{label: T("pair.find"), act: a.startDiscover, kind: btnPrimary},
		{label: T("pair.manual"), act: func() { a.go_(a.keyboard(T("pair.address"), addressKeys, "http://", a.chooseServer)) }},
	}}
	if paired {
		p.title = T("pair.change")
		p.lines = []line{{text: T("home.server", a.c.st.Server), col: colText}}
		p.buttons = append(p.buttons,
			button{label: T("pair.forget"), act: func() {
				_ = a.c.Forget()
				a.go_(func() page {
					return page{title: T("pair.forgotten"), lines: []line{{text: T("pair.forgotten_text"), col: colText}}, buttons: []button{
						{label: T("pair.connect"), act: func() { a.go_(a.pairMenu) }}, {label: T("btn.exit"), act: func() { a.quit = true }}}}
				})
			}},

			button{label: T("btn.back"), kind: btnBack, act: func() { a.go_(a.settings) }})
	} else {
		if a.c.Damaged != "" { // the saved pairing was lost to a damaged card: say why it asks again
			p.title = T("state.damaged.title")
			p.lines = []line{{text: T("state.damaged"), col: colText}}
		}
		p.buttons = append(p.buttons, button{label: T("btn.exit"), act: func() { a.quit = true }})
	}
	return p
}

func (a *App) back() func() {
	return func() { a.gen++; a.go_(a.pairMenu) }
}

func (a *App) startDiscover() {
	a.gen++
	gen := a.gen
	a.go_(func() page {
		return page{title: T("pair.finding"), lines: []line{{text: T("pair.finding_text"), col: colDim}},
			buttons: []button{{label: T("btn.cancel"), act: a.back()}}}
	})
	go func() {
		list, err := discover(4 * time.Second)
		a.later <- func() {
			if gen != a.gen {
				return
			}
			p := page{title: T("pair.found"), sub: T("pair.found_sub"), buttons: []button{}}
			for _, f := range list {
				if f.PairOpen {
					p.lines = []line{{text: T("pair.star"), col: colDim}}
				}
			}
			if err != nil {
				p.lines = []line{{text: err.Error(), col: colErr}}
			} else if len(list) == 0 {
				p.lines = []line{{text: T("pair.found_none"), col: colText}}
			}
			for _, f := range list {
				label := f.Name
				if f.PairOpen {
					label += " *"
				}
				_, addr := serverName(f.URL)
				label += "\n" + addr
				url := f.URL
				p.buttons = append(p.buttons, button{label: label, act: func() { a.chooseServer(url) }})
			}
			p.buttons = append(p.buttons, button{label: T("pair.again"), act: a.startDiscover}, button{label: T("btn.back"), kind: btnBack, act: a.back()})
			a.go_(func() page { return p })
		}
	}()
}

// chooseServer: ask the server to approve this player, or type the pairing code shown in its web UI.
func (a *App) chooseServer(url string) {
	url = normalizeServer(url)
	a.go_(func() page {
		return page{title: T("pair.how"), lines: []line{{text: url, col: colText}, {text: T("pair.how_text"), col: colDim}}, buttons: []button{
			{label: T("pair.ask"), act: func() { a.startRequest(url) }, kind: btnPrimary},
			{label: T("pair.code"), act: func() {
				a.go_(a.keypad(T("pair.code_title"), func(code string) { a.pairWithCode(url, code) }))
			}},
			{label: T("btn.back"), kind: btnBack, act: a.back()},
		}}
	})
}

func (a *App) pairWithCode(url, code string) {
	a.go_(func() page {
		return page{title: T("pair.title"), lines: []line{{text: T("pair.with", url), col: colText}}}
	})
	a.render()
	if err := a.c.Pair(url, code, playerName); err != nil {
		a.go_(a.pairError(err))
		return
	}
	a.paired()
}

func (a *App) paired() {
	a.set = loadSettings(a.c.data) // the server may have given back this player's settings
	a.online.Store(ptr(T("home.online")))
	a.waitRegistration(a.message(T("pair.done"), T("pair.done_text"), false))
}

// waitRegistration: while the server says this player's setup is not finished there (name, what it gets), show
// "finish setting up on the server" and poll; then go on to `after`. Never an error for this state.
func (a *App) waitRegistration(after func() page) {
	a.gen++
	gen := a.gen
	name := a.shownName()
	a.go_(func() page {
		_, addr := serverName(a.c.st.Server)
		return page{title: T("reg.title"), lines: []line{
			{text: T("reg.text"), col: colSoft},
			{text: T("reg.where", name), col: colTeal2, box: boxNote}, {text: addr, col: colNoteTxt, size: sizeSmall, box: boxNote},
			{text: T("reg.waits"), col: colDim}},
			buttons: []button{{label: T("btn.exit"), act: func() { a.quit = true }}}}
	})
	lp := &Client{st: State{Server: a.c.st.Server, Token: a.c.st.Token}, http: newHTTPClient(15 * time.Second)}
	go func() {
		for {
			var st struct {
				Registration string `json:"registration"`
				Name         string `json:"name"`
			}
			err := lp.call("GET", "status", nil, &st)
			if err == nil && st.Registration != "pending" {
				a.later <- func() {
					if st.Name != "" && st.Name != a.c.st.Name {
						a.c.st.Name = st.Name
						_ = a.c.save()
					}
					if gen == a.gen {
						a.go_(after)
					}
				}
				return
			}
			time.Sleep(3 * time.Second)
			if a.genNow() != gen {
				return
			}
		}
	}()
}

func (a *App) pairError(err error) func() page {
	text := err.Error()
	var pe *ProtoError
	if errors.As(err, &pe) {
		switch pe.Code {
		case "request_rejected":
			text = T("pair.rejected")
		case "request_expired":
			text = T("pair.expired")
		case "pair_requests_disabled":
			text = T("pair.requests_off")
		case "clients_disabled":
			text = T("pair.clients_off")
		}
	}
	return func() page {
		return page{title: T("pair.failed"), lines: []line{{text: text, col: colErr}}, buttons: []button{
			{label: T("btn.back"), kind: btnBack, act: a.back()}, {label: T("btn.exit"), act: func() { a.quit = true }}}}
	}
}

// startRequest: registration mode. The server shows "a player wants to connect" with the same short code
// as this screen; the app polls until the user approves or rejects it there.
func (a *App) startRequest(url string) {
	a.gen++
	gen := a.gen
	code := ""
	a.go_(func() page {
		ls := []line{{text: T("pair.asking", url), col: colDim}}
		if code != "" {
			_, addr := serverName(url)
			ls = []line{{text: T("pair.approve_on_server"), col: colSoft},
				{text: code, col: colTeal2, size: sizeCode, box: boxNote}, {text: addr, col: colNoteTxt, size: sizeSmall, box: boxNote}}
		}
		return page{title: T("pair.waiting"), lines: ls, buttons: []button{{label: T("btn.cancel"), act: a.back()}}}
	})
	go func() {
		q, err := a.c.RequestPair(url, playerName)
		a.later <- func() {
			if gen == a.gen && err == nil {
				code = q.DisplayCode
			}
		}
		for err == nil {
			var ok bool
			if ok, err = a.c.PollPair(q); ok {
				break
			}
			time.Sleep(time.Duration(q.PollS) * time.Second)
			if a.genNow() != gen {
				return
			}
		}
		a.later <- func() {
			if gen != a.gen {
				return
			}
			if err != nil {
				a.go_(a.pairError(err))
			} else {
				a.paired()
			}
		}
	}()
}

func (a *App) genNow() int {
	ch := make(chan int, 1)
	a.later <- func() { ch <- a.gen }
	return <-ch
}

// on-screen keyboards
var (
	codeKeys    = strings.Split("123456789", "") // the number pad: 1–9 in three rows, then delete, 0, connect
	addressKeys = strings.Split("1234567890.:/abcdefghijklmnopqrstuvwxyz-", "")
)

func (a *App) keyboard(title string, keys []string, start string, ok func(string)) func() page {
	text := start
	return func() page {
		p := page{title: title, field: text}
		for _, k := range keys {
			k := k
			p.buttons = append(p.buttons, button{label: k, act: func() { text += k }})
		}
		p.buttons = append(p.buttons, button{label: T("pair.delete"), act: func() {
			if r := []rune(text); len(r) > 0 {
				text = string(r[:len(r)-1])
			}
		}})
		p.grid = len(p.buttons)
		p.buttons = append(p.buttons, button{label: T("btn.ok"), act: func() { ok(text) }, kind: btnPrimary}, button{label: T("btn.back"), kind: btnBack, act: a.back()})
		return p
	}
}

// wifiPage: shown at start when Wi-Fi is not connected; continue with `after` once it is (or anyway).
func (a *App) wifiPage(on bool, after func() page) func() page {
	title, status, busy := T("wifi.off_title"), T("wifi.off"), false
	if on {
		title, status = T("wifi.on_title"), T("wifi.not_connected")
	}
	cont := func() { a.go_(after) }
	return func() page {
		if busy {
			return page{title: title, centered: true, icon: iconWifiOff, lines: []line{{text: T("wifi.turning_on")}},
				buttons: []button{{label: T("btn.exit"), act: func() { a.quit = true }}}}
		}
		return page{title: title, centered: true, icon: iconWifiOff, lines: []line{{text: status}}, foot: T("wifi.foot"), buttons: []button{
			{label: T("wifi.turn_on"), kind: btnPrimary, act: func() {
				busy = true
				go func() {
					err := wifiOn(20 * time.Second)
					a.later <- func() {
						busy = false
						switch {
						case err == nil:
							cont()
						case errors.Is(err, errNoWifiScript):
							status = T("wifi.by_hand")
						case errors.Is(err, errWifiTimeout):
							status = T("wifi.timeout")
						default:
							status = err.Error()
						}
					}
				}()
			}},
			{label: T("wifi.continue"), act: cont},
			{label: T("btn.exit"), act: func() { a.quit = true }},
		}}
	}
}

// previewPage: what the sync will do; Start / Cancel. A sync the server holds for confirmation (many
// removals, not enough space) says why and is confirmed here.
func (a *App) previewPage(p Preview, ans chan<- bool) func() page {
	answer := func(ok bool) func() {
		return func() {
			ans <- ok
			if ok {
				a.go_(a.syncPage)
			} else {
				a.go_(func() page { return page{title: T("preview.cancelling")} })
			}
		}
	}
	return func() page {
		row := func(label, value string) line {
			return line{text: label, right: value, size: sizeRow, box: boxCard}
		}
		ls := []line{row(T("preview.t_add"), fmt.Sprintf("+%d · %s", p.FetchCount, mb(p.FetchBytes))),
			row(T("preview.t_remove"), fmt.Sprint(p.DeleteCount))}
		if p.Playlists > 0 {
			ls = append(ls, row(T("preview.t_lists"), fmt.Sprint(p.Playlists)))
		}
		warn := func(title, text string) {
			ls = append(ls, line{text: title, size: sizeRowSemi, col: colWarnTxt, box: boxWarn})
			if text != "" {
				ls = append(ls, line{text: text, size: sizeSmall, col: colWarnSub, box: boxWarn})
			}
		}
		if p.Confirm {
			warn(T("preview.confirm_t"), upFirst(p.Reason)+".")
		}
		if p.SkippedForSpace > 0 {
			warn(T("preview.skipped", p.SkippedForSpace), "")
		}
		if _, free := freeSpace(a.c.root); free > 0 {
			if p.FetchBytes > free {
				warn(T("preview.no_space", mb(free)), "")
			}
			ls = append(ls, line{text: T("preview.space_l"), right: T("preview.free", gbShort(max(0, free-p.FetchBytes))), size: sizeSmall, col: colSoft})
		}
		start := T("preview.start")
		if p.Confirm {
			start = T("preview.confirm_start")
		}
		name, _ := serverName(a.c.st.Server)
		return page{title: T("preview.title"), sub: name + " · " + T("preview.checked"), lines: ls, buttons: []button{
			{label: start, act: answer(true), kind: btnPrimary, half: true}, {label: T("btn.cancel"), act: answer(false), half: true}}}
	}
}

func (a *App) sendDiagnostics() {
	a.go_(func() page {
		return page{title: T("diag.title"), lines: []line{{text: T("diag.sending"), col: colDim}}}
	})
	a.render()
	back := []button{{label: T("btn.back"), kind: btnBack, act: func() { a.go_(a.about) }}}
	if err := a.c.SendDiagnostics(a.set); err != nil {
		a.go_(func() page {
			return page{title: T("diag.title"), lines: []line{{text: err.Error(), col: colErr}}, buttons: back}
		})
		return
	}
	a.go_(func() page {
		return page{title: T("diag.title"), lines: []line{{text: T("diag.sent"), col: colText}}, buttons: back}
	})
}

// openTemplates: the server's templates with on/off; selecting one switches it on the server.
func (a *App) openTemplates() {
	var list []Template
	msg := line{text: T("tpl.loading"), col: colDim}
	busy := true
	load := func(f func() ([]Template, error)) {
		busy = true
		go func() {
			l, err := f()
			a.later <- func() {
				busy = false
				if err != nil {
					msg = line{text: err.Error(), col: colErr}
					return
				}
				list, msg = l, line{text: T("tpl.hint"), col: colDim}
			}
		}()
	}
	load(a.c.Templates)
	a.go_(func() page {
		p := page{title: T("tpl.title"), lines: []line{msg}}
		for _, tp := range list {
			tp := tp
			mark := "[ ] "
			if tp.On {
				mark = "[x] "
			}
			p.buttons = append(p.buttons, button{label: mark + tp.Name, act: func() {
				if busy {
					return
				}
				on := []string{}
				for _, x := range list {
					if x.On != (x.ID == tp.ID) {
						on = append(on, x.ID)
					}
				}
				load(func() ([]Template, error) { return a.c.SetTemplates(on) })
			}})
		}
		return p.withBack(button{label: T("btn.back"), kind: btnBack, act: func() { a.go_(a.settings) }})
	})
}

func (p page) withBack(b button) page {
	p.buttons = append(p.buttons, b)
	return p
}

// keypad: the pairing code (digits only) on a phone-style number pad: 1 2 3 / 4 5 6 / 7 8 9 / delete 0 connect; Back.
func (a *App) keypad(title string, ok func(string)) func() page {
	text := ""
	digit := func(k string) button {
		return button{label: k, act: func() {
			if len(text) < 8 {
				text += k
			}
		}}
	}
	return func() page {
		p := page{title: title, sub: T("pair.code_hint"), field: show4(text), cols: 3}
		for _, k := range codeKeys {
			p.buttons = append(p.buttons, digit(k))
		}
		p.buttons = append(p.buttons,
			button{label: "⌫", act: func() {
				if len(text) > 0 {
					text = text[:len(text)-1]
				}
			}},
			digit("0"),
			button{label: T("btn.connect"), act: func() { ok(text) }, kind: btnPrimary})
		p.grid = len(p.buttons)
		p.buttons = append(p.buttons, button{label: T("btn.back"), kind: btnBack, act: a.back()})
		return p
	}
}

// show4: "12345678" → "1234-5678" as the server shows it.
func show4(s string) string {
	if len(s) > 4 {
		return s[:4] + "-" + s[4:]
	}
	return s
}
