// SPDX-License-Identifier: GPL-3.0-or-later
package main

// The release ships two binaries: syncmusic-compas uses the FPU (hard float, much faster drawing) and
// syncmusic-compas-softfloat emulates floats in software. The hard-float one starts the soft-float one when the
// CPU reports no FPU.

import (
	"os"
	"path/filepath"
	"strings"
	"syscall"
)

// floatABI is set at build time (-ldflags -X main.floatABI=hard).
var floatABI = "soft"

const softBinary = "syncmusic-compas-softfloat"

// cpuModel: the "cpu model" line of /proc/cpuinfo (MIPS names the FPU there, e.g. "… FPU V0.0").
func cpuModel(cpuinfo string) string {
	for _, l := range strings.Split(cpuinfo, "\n") {
		if k, v, ok := strings.Cut(l, ":"); ok && strings.TrimSpace(k) == "cpu model" {
			return strings.TrimSpace(v)
		}
	}
	return ""
}

// needSoftFloat: a hard-float binary on a MIPS CPU that does not report an FPU.
func needSoftFloat(abi, goarch, model string) bool {
	return abi == "hard" && strings.HasPrefix(goarch, "mips") && model != "" && !strings.Contains(model, "FPU")
}

// fpuFallback replaces the process with the soft-float binary when needed (only returns when it does not).
func fpuFallback(goarch string) {
	b, _ := os.ReadFile("/proc/cpuinfo")
	if !needSoftFloat(floatABI, goarch, cpuModel(string(b))) {
		return
	}
	exe, err := os.Executable()
	if err != nil {
		return
	}
	soft := filepath.Join(filepath.Dir(exe), softBinary)
	if _, err := os.Stat(soft); err != nil {
		return
	}
	_ = syscall.Exec(soft, append([]string{soft}, os.Args[1:]...), append(os.Environ(), "SYNCMUSIC_FPU_FALLBACK=1"))
}
