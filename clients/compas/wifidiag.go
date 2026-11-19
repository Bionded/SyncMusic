// SPDX-License-Identifier: GPL-3.0-or-later
package main

// Keeping the connection while the screen is off: the app holds a kernel wake lock while it runs (where the kernel
// has /sys/power/wake_lock) and switches the screen off by brightness only (no framebuffer blank, which may power
// down drivers on this kernel). While the screen is off it logs the Wi-Fi state and whether the server answers
// every 10 s into startup.log, so a connection that still drops can be traced on the player.

import (
	"net"
	"os"
	"os/exec"
	"strings"
	"time"
)

const wakeLockName = "syncmusic"

// holdWakeLock / releaseWakeLock: a missing interface is fine (the player's own suspend cannot fire while an app
// runs anyway).
func holdWakeLock() {
	if err := os.WriteFile(sysRoot("/sys/power/wake_lock"), []byte(wakeLockName), 0o644); err == nil {
		step("wake lock %q held", wakeLockName)
	}
}

func releaseWakeLock() {
	_ = os.WriteFile(sysRoot("/sys/power/wake_unlock"), []byte(wakeLockName), 0o644)
}

// wifiDiag: one line about wlan0 (link state, address, wpa state, power save).
func wifiDiag() string {
	n := sysRoot("/sys/class/net/wlan0/")
	parts := []string{"operstate=" + readTrim(n+"operstate"), "carrier=" + readTrim(n+"carrier")}
	if ifc, err := net.InterfaceByName("wlan0"); err == nil {
		if as, err := ifc.Addrs(); err == nil && len(as) > 0 {
			parts = append(parts, "ip="+as[0].String())
		}
	}
	if out, err := exec.Command("wpa_cli", "-i", "wlan0", "status").Output(); err == nil {
		for _, l := range strings.Split(string(out), "\n") {
			if strings.HasPrefix(l, "wpa_state=") {
				parts = append(parts, l)
			}
		}
	}
	if out, err := exec.Command("iw", "dev", "wlan0", "get", "power_save").Output(); err == nil {
		parts = append(parts, strings.TrimSpace(string(out)))
	}
	if b, err := os.ReadFile(sysRoot("/sys/power/wake_lock")); err == nil {
		parts = append(parts, "wake_locks=["+strings.TrimSpace(string(b))+"]")
	}
	return strings.Join(parts, " ")
}

// watchWhileOff logs the Wi-Fi state and a server check every 10 s until off() says the screen is on again.
func watchWhileOff(c *Client, off func() bool) {
	if c == nil || c.st.Server == "" {
		return
	}
	probe := &Client{st: State{Server: c.st.Server}, http: newHTTPClient(5 * time.Second)}
	for off() {
		res := "server ok"
		if err := probe.call("GET", "hello", nil, nil); err != nil {
			res = "server unreachable: " + err.Error()
		}
		step("screen off: %s, %s", wifiDiag(), res)
		time.Sleep(10 * time.Second)
	}
}
