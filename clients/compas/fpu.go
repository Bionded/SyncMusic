// SPDX-License-Identifier: GPL-3.0-or-later
package main

// Each release zip holds one build of the app as `syncmusic-compas`: the hard-float build (uses the FPU, recommended)
// or the soft-float build (floats in software, the fallback for players where the hard-float one does not start).

import "strings"

// floatABI is set at build time (-ldflags -X main.floatABI=hard); the server keeps offering updates of the same build.
var floatABI = "soft"

// cpuModel: the "cpu model" line of /proc/cpuinfo (MIPS names the FPU there, e.g. "… FPU V0.0").
func cpuModel(cpuinfo string) string {
	for _, l := range strings.Split(cpuinfo, "\n") {
		if k, v, ok := strings.Cut(l, ":"); ok && strings.TrimSpace(k) == "cpu model" {
			return strings.TrimSpace(v)
		}
	}
	return ""
}
