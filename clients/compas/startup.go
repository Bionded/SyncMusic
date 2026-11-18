// SPDX-License-Identifier: GPL-3.0-or-later
package main

// Startup log: when the app dies before it draws its first screen, the player's log can stay empty (a kill by the
// kernel leaves nothing). Each startup step therefore goes to <app data>/startup.log as well, synced to the card at
// once, with the process's memory use; a Go crash (panic, fatal error, fault) writes its stack traces there too.
// The previous launch's file is kept as startup.log.1.

import (
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"runtime"
	"runtime/debug"
	"strings"
	"sync"
	"syscall"
	"time"
)

var startLog struct {
	sync.Mutex
	f *os.File
}

// openStartupLog starts the startup log in the app's data folder (no-op without COMPAS_APP_DATA).
func openStartupLog() {
	debug.SetTraceback("all")
	data := os.Getenv("COMPAS_APP_DATA")
	if data == "" {
		return
	}
	_ = os.MkdirAll(data, 0o755)
	p := filepath.Join(data, "startup.log")
	_ = os.Rename(p, p+".1")
	f, err := os.OpenFile(p, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)
	if err != nil {
		fmt.Fprintln(os.Stderr, "startup log not opened:", err)
		return
	}
	startLog.f = f
	_ = debug.SetCrashOutput(f, debug.CrashOptions{})
	step("syncmusic-compas %s starting: pid %d, %s/%s, screen %sx%s, board %s", version, os.Getpid(), runtime.GOOS,
		runtime.GOARCH, os.Getenv("COMPAS_SCREEN_WIDTH"), os.Getenv("COMPAS_SCREEN_HEIGHT"), os.Getenv("COMPAS_BOARD"))
	cpu, _ := os.ReadFile("/proc/cpuinfo")
	step("float %s (fallback %v), cpu model %q", floatABI, os.Getenv("SYNCMUSIC_FPU_FALLBACK") != "", cpuModel(string(cpu)))
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGTERM, syscall.SIGHUP, syscall.SIGINT, syscall.SIGQUIT)
	go func() {
		s := <-sig
		step("stopped by signal %v", s)
		os.Exit(1)
	}()
}

// step writes one startup step to stderr (the player's log) and to the startup log, synced to the card.
func step(format string, args ...any) {
	line := time.Now().Format("15:04:05.000") + " " + fmt.Sprintf(format, args...) + " (" + memUse() + ")"
	fmt.Fprintln(os.Stderr, "startup:", line)
	startLog.Lock()
	defer startLog.Unlock()
	if startLog.f != nil {
		_, _ = startLog.f.WriteString(line + "\n")
		_ = startLog.f.Sync()
	}
}

// memUse: the process's resident memory and the memory still available on the player.
func memUse() string {
	field := func(path, name string) string {
		b, _ := os.ReadFile(path)
		for _, l := range strings.Split(string(b), "\n") {
			if strings.HasPrefix(l, name+":") {
				return strings.Join(strings.Fields(l[len(name)+1:]), " ")
			}
		}
		return "?"
	}
	return "rss " + field("/proc/self/status", "VmRSS") + ", available " + field("/proc/meminfo", "MemAvailable")
}
