// SPDX-License-Identifier: GPL-3.0-or-later
package main

import (
	"fmt"
	"image"
	"image/color"
	"image/draw"
	"image/png"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// mockup: the screen design each sample page is compared with (SYNCMUSIC_MOCKUPS=<dir>: side-<page>.png).
var mockup = map[string]string{"home": "Main", "sync": "Sync", "preview": "Preview", "done": "Done", "settings": "Settings",
	"found": "Pair", "keypad": "Code", "wifi-off": "WifiOff"}

// TestScreenshots draws sample pages as compas-app-<page>.png (480x800) into $SYNCMUSIC_SHOTS (the user guide's
// pictures come from here); skipped when it is not set.
func TestScreenshots(t *testing.T) {
	dir := os.Getenv("SYNCMUSIC_SHOTS")
	if dir == "" {
		t.Skip("SYNCMUSIC_SHOTS not set")
	}
	f, err := loadFonts()
	if err != nil {
		t.Fatal(err)
	}
	t0 := time.Now()
	p := Progress{Phase: phaseDownload, Files: 37, FilesTotal: 412, Bytes: 912e6, BytesTotal: 9.8e9,
		File: "Music/Океан Ельзи/Земля/07 Мало мені.flac", FileSize: 38.2e6, Started: t0.Add(-7 * time.Minute),
		Received: 37, Removed: 5, Playlists: 2, Sent: map[string]int{"play": 14, "favourite": 3, "position": 2}}
	pages := map[string]page{
		"sync": {title: T("sync.title_short"), showStatus: true, lines: syncLines(p, t0),
			buttons: []button{{label: T("btn.stop"), kind: btnDanger}}},
	}
	fakeSys(t, map[string]string{
		"sys/class/power_supply/battery/type": "Battery", "sys/class/power_supply/battery/capacity": "64",
		"sys/class/power_supply/battery/status": "Charging", "var/run/wpa_supplicant/wlan0": "",
		"usr/bin/wpa_cli": "#!/bin/sh\necho wpa_state=COMPLETED; echo ip_address=192.0.2.5\n"})
	_ = os.Chmod(sysRoot("/usr/bin/wpa_cli"), 0o755)
	a := &App{c: &Client{root: t.TempDir(), st: State{Server: "http://192.0.2.10:8080/", DeviceID: "c0ffee12",
		LastSync: "2026-11-16 21:40", LastSummary: "Received: 12 new, 3 removed. Sent: 4 plays, 1 heart, 2 positions."}},
		set: defaultSettings()}
	a.online.Store(ptr(T("home.online")))
	a.c.st.LastGrid = endGrid(Result{Fetched: 12, Deleted: 3, Sent: map[string]int{"play": 5, "favourite": 2, "position": 3}}, &downPlan{}, 1)
	pages["home"] = a.home()
	pages["settings"] = a.settings()
	a.c.st.History = []HistoryEntry{{"2026-11-16 21:40", T("end.finished"), "Received: 12 new, 3 removed."}, {"2026-11-15 08:02", T("end.battery"), "Received: 40 new, 0 removed. 120 not done (next sync continues)."}}
	pages["history"] = a.history()
	kp := a.keypad("192.0.2.10", func(string) {})
	kp()
	for _, i := range []int{1, 2, 3, 4} { // type 2345
		kp().buttons[i].act()
	}
	pages["keypad"] = kp()
	pages["preview"] = a.previewPage(Preview{Totals: Totals{FetchCount: 120, FetchBytes: 2.3e9, DeleteCount: 84},
		Playlists: 3, Confirm: true, Reason: "removes 84 of 270 items (more than 20 %)"}, nil)()
	a.c.st.Name = "Плеєр у вітальні"
	a.waitRegistration(func() page { return page{} }) // its poll goroutine fails against the test address
	pages["setup"] = a.build()
	a.c.st.Name = ""
	a.c.st.Name = "Плеєр у вітальні"
	pages["settings-name"] = a.settings()
	a.c.st.Name = ""
	pages["keyboard"] = a.keyboard(T("pair.address"), addressKeys, "http://192.0.2.", func(string) {})()
	pages["waiting"] = page{title: T("pair.waiting"), lines: []line{{text: T("pair.approve_on_server"), col: colSoft},
		{text: "4821", col: colTeal2, size: sizeCode, box: boxNote}, {text: "192.0.2.10:8080", col: colNoteTxt, size: sizeSmall, box: boxNote}},
		buttons: []button{{label: T("btn.cancel")}, {label: T("btn.back"), kind: btnBack}}}
	pages["found"] = page{title: T("pair.found"), sub: T("pair.found_sub"), buttons: []button{
		{label: "Домашній сервер\n192.0.2.10:8080"}, {label: "nas\n192.0.2.20:8765"},
		{label: T("pair.again")}, {label: T("btn.back"), kind: btnBack}}}
	pages["wifi-off"] = a.wifiPage(false, a.home)()
	done := Result{Fetched: 120, Deleted: 84, Generation: 2, Sent: map[string]int{"play": 6, "favourite": 2, "position": 5}}
	pages["done"] = page{title: T("end.finished"), icon: iconCheck, sub: "5 min · 2.3 GB", foot: T("end.restart_in", 10),
		lines:   []line{{grid: endGrid(done, &downPlan{}, 3), box: boxCard}, {text: T("end.need_restart"), col: colNoteTxt, box: boxNote}},
		buttons: []button{{label: T("end.btn_restart"), kind: btnPrimary}, {label: T("btn.poweroff"), half: true}, {label: T("btn.cancel"), half: true}}}
	var many []button
	for i := range 30 {
		many = append(many, button{label: fmt.Sprintf("Шаблон %d", i+1), act: nil})
	}
	pages["long-list"] = page{title: T("tpl.title"), lines: []line{{text: T("tpl.hint"), col: colDim}}, buttons: many}
	_ = os.MkdirAll(dir, 0o755)
	for name, pg := range pages {
		raw := filepath.Join(t.TempDir(), name+".rgb565")
		t.Setenv("SYNCMUSIC_FB", raw)
		t.Setenv("COMPAS_SCREEN_WIDTH", "480")
		t.Setenv("COMPAS_SCREEN_HEIGHT", "800")
		s, err := openScreen(f)
		if err != nil {
			t.Fatal(err)
		}
		status := "Wi-Fi ●   64%"
		if name == "sync" {
			status = "⚡ 66% · " + T("sync.stays_on")
		}
		s.drawPage(pg, 0, status)
		s.file.Close()
		b, _ := os.ReadFile(raw)
		img := image.NewRGBA(image.Rect(0, 0, 480, 800))
		for i := 0; i+1 < len(b); i += 2 {
			v := uint16(b[i]) | uint16(b[i+1])<<8
			img.Set(i/2%480, i/2/480, color.RGBA{uint8(v>>11)<<3 | uint8(v>>11)>>2, uint8(v>>5&63)<<2 | uint8(v>>5&63)>>4, uint8(v&31)<<3 | uint8(v&31)>>2, 255})
		}
		if m := os.Getenv("SYNCMUSIC_MOCKUPS"); m != "" && mockup[name] != "" {
			if f, err := os.Open(filepath.Join(m, mockup[name]+".png")); err == nil {
				if mi, err := png.Decode(f); err == nil {
					both := image.NewRGBA(image.Rect(0, 0, 980, 800))
					draw.Draw(both, both.Bounds(), image.NewUniform(color.White), image.Point{}, draw.Src)
					draw.Draw(both, image.Rect(0, 0, 480, 800), mi, image.Point{}, draw.Src)
					draw.Draw(both, image.Rect(500, 0, 980, 800), img, image.Point{}, draw.Src)
					if o, err := os.Create(filepath.Join(dir, "side-"+name+".png")); err == nil {
						_ = png.Encode(o, both)
						o.Close()
					}
				}
				f.Close()
			}
		}
		out, err := os.Create(filepath.Join(dir, "compas-app-"+name+".png"))
		if err != nil {
			t.Fatal(err)
		}
		_ = png.Encode(out, img)
		out.Close()
	}
}
