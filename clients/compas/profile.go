// SPDX-License-Identifier: GPL-3.0-or-later
package main

import (
	"os"
	"path/filepath"
	"runtime/pprof"
	"strconv"
	"strings"
	"sync"
	"time"
)

var profStop = func() {}

// startProfile: when the file "profile" is in the app's data folder, the app records a CPU profile into cpu.pprof
// next to it for the number of seconds in that file (default 60) or until it exits, to find slow drawing on the
// player itself (go tool pprof syncmusic-compas cpu.pprof).
func startProfile(data string) {
	b, err := os.ReadFile(filepath.Join(data, "profile"))
	if err != nil {
		return
	}
	secs, err := strconv.Atoi(strings.TrimSpace(string(b)))
	if err != nil || secs <= 0 {
		secs = 60
	}
	f, err := os.Create(filepath.Join(data, "cpu.pprof"))
	if err != nil {
		step("profile not started: %v", err)
		return
	}
	if err := pprof.StartCPUProfile(f); err != nil {
		step("profile not started: %v", err)
		_ = f.Close()
		return
	}
	step("cpu profile for %d s into cpu.pprof", secs)
	var once sync.Once
	profStop = func() {
		once.Do(func() {
			pprof.StopCPUProfile()
			_ = f.Close()
			step("cpu profile written")
		})
	}
	time.AfterFunc(time.Duration(secs)*time.Second, profStop)
}
