// SPDX-License-Identifier: GPL-3.0-or-later
// Read-mostly access to the player's hardware through sysfs: battery, backlight, Wi-Fi state, free space.
// `SYNCMUSIC_SYSFS` = a fake root for tests (it replaces "/" in front of /sys, /var/run and /usr/bin).
// The charge limiter of the firmware is never touched (it works on the PMIC directly, not through us).
package main

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

func sysRoot(p string) string { return filepath.Join(os.Getenv("SYNCMUSIC_SYSFS"), p) }

func readTrim(p string) string {
	b, err := os.ReadFile(p)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(b))
}

func readInt(p string) (int, bool) {
	n, err := strconv.Atoi(readTrim(p))
	return n, err == nil
}

// battery: capacity in % (-1 = unknown) and whether it charges (or is full on the charger).
func battery() (int, bool) {
	dirs, _ := filepath.Glob(sysRoot("/sys/class/power_supply/*"))
	best := ""
	for _, d := range dirs {
		if readTrim(filepath.Join(d, "type")) != "Battery" {
			continue
		}
		if best == "" || filepath.Base(d) == "battery" {
			best = d
		}
	}
	if best == "" {
		return -1, false
	}
	pct, ok := readInt(filepath.Join(best, "capacity"))
	if !ok {
		pct = -1
	}
	st := readTrim(filepath.Join(best, "status"))
	return pct, st == "Charging" || st == "Full"
}

// backlight dims and switches the screen off; restore() puts back what it found (the player resyncs too).
type backlight struct {
	dir       string // /sys/class/backlight/<first>
	max, orig int
	mode      int
}

func openBacklight() *backlight {
	dirs, _ := filepath.Glob(sysRoot("/sys/class/backlight/*"))
	if len(dirs) == 0 {
		return &backlight{}
	}
	b := &backlight{dir: dirs[0]}
	b.max, _ = readInt(filepath.Join(b.dir, "max_brightness"))
	b.orig, _ = readInt(filepath.Join(b.dir, "brightness"))
	if b.orig <= 0 {
		b.orig = b.max * 6 / 10
	}
	return b
}

func (b *backlight) write(name, v string) {
	_ = os.WriteFile(name, []byte(v), 0o644)
}

// set switches to on / dim (percent of the original brightness) / off.
func (b *backlight) set(mode, dimPercent int) {
	if b.dir == "" || mode == b.mode {
		return
	}
	switch mode {
	case screenOff:
		// brightness only: blanking the framebuffer may power drivers down (and with them Wi-Fi) on this kernel
		b.write(filepath.Join(b.dir, "brightness"), "0")
	default:
		if b.mode == screenOff {
			// a screen blanked by an older app build or the player: unblank first, then the brightness
			b.write(sysRoot("/sys/class/graphics/fb0/blank"), "0")
		}
		v := b.orig
		if mode == screenDim {
			v = max(1, b.orig*dimPercent/100)
		}
		b.write(filepath.Join(b.dir, "brightness"), strconv.Itoa(v))
	}
	b.mode = mode
}

func (b *backlight) restore() { b.set(screenOn, 100) }

// wifi: "on" when wpa_supplicant runs for wlan0, "connected" when it is associated with an address.
func wifiState() (on, connected bool) {
	if _, err := os.Stat(sysRoot("/var/run/wpa_supplicant/wlan0")); err != nil {
		return false, false
	}
	out, err := exec.Command(tool("wpa_cli"), "-i", "wlan0", "status").Output()
	if err != nil {
		return true, false
	}
	s := string(out)
	return true, strings.Contains(s, "wpa_state=COMPLETED") && strings.Contains(s, "ip_address=")
}

// freeSpace of the card: total and free bytes.
func freeSpace(root string) (total, free int64) {
	var st syscall.Statfs_t
	if syscall.Statfs(root, &st) != nil {
		return 0, 0
	}
	return int64(st.Blocks) * int64(st.Bsize), int64(st.Bavail) * int64(st.Bsize)
}

func gb(n int64) string {
	return strconv.FormatFloat(float64(n)/1e9, 'f', 1, 64) + " GB"
}

// tool: the player's program (under the fake root in tests, else found on PATH).
func tool(name string) string {
	if p := sysRoot("/usr/bin/" + name); os.Getenv("SYNCMUSIC_SYSFS") != "" {
		return p
	}
	return name
}

// wifiOn turns Wi-Fi on the way the player does (its wifi_on.sh), waits up to `wait` for a connection and
// asks for an address when it is associated without one. errNoWifiScript: this player does it differently
// (the user turns Wi-Fi on in the player's settings).
func wifiOn(wait time.Duration) error {
	script := sysRoot("/usr/bin/wifi_on.sh")
	if _, err := os.Stat(script); err != nil {
		return errNoWifiScript
	}
	if out, err := exec.Command("/bin/sh", script).CombinedOutput(); err != nil {
		return fmt.Errorf("wifi_on.sh: %v %s", err, strings.TrimSpace(string(out)))
	}
	dhcp := false
	for end := time.Now().Add(wait); time.Now().Before(end); time.Sleep(500 * time.Millisecond) {
		on, ok := wifiState()
		if ok {
			return nil
		}
		if on && !dhcp && associated() {
			dhcp = true
			_ = exec.Command(tool("udhcpc"), "-b", "-i", "wlan0", "-q").Run()
		}
	}
	return errWifiTimeout
}

var (
	errNoWifiScript = errors.New("no wifi_on.sh")
	errWifiTimeout  = errors.New("Wi-Fi did not connect")
)

func associated() bool {
	out, _ := exec.Command(tool("wpa_cli"), "-i", "wlan0", "status").Output()
	return strings.Contains(string(out), "wpa_state=COMPLETED")
}
