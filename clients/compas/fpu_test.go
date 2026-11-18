// SPDX-License-Identifier: GPL-3.0-or-later
package main

import "testing"

func TestNeedSoftFloat(t *testing.T) {
	info := "system type\t\t: X1600\nmachine\t\t\t: Example\nprocessor\t\t: 0\ncpu model\t\t: Ingenic XBurst V4.15  FPU V0.0\n"
	if m := cpuModel(info); m != "Ingenic XBurst V4.15  FPU V0.0" {
		t.Fatalf("model %q", m)
	}
	for _, c := range []struct {
		abi, arch, model string
		want             bool
	}{
		{"hard", "mipsle", "Ingenic XBurst V4.15  FPU V0.0", false},
		{"hard", "mipsle", "Ingenic XBurst V4.15", true},
		{"hard", "mipsle", "", false}, // unknown: keep the fast binary
		{"soft", "mipsle", "Ingenic XBurst V4.15", false},
		{"hard", "arm64", "whatever", false},
	} {
		if got := needSoftFloat(c.abi, c.arch, c.model); got != c.want {
			t.Errorf("%+v: got %v", c, got)
		}
	}
}
