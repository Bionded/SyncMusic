// SPDX-License-Identifier: GPL-3.0-or-later
package main

import "testing"

func TestCPUModel(t *testing.T) {
	info := "system type\t\t: X1600\nmachine\t\t\t: Example\nprocessor\t\t: 0\ncpu model\t\t: Ingenic XBurst V4.15  FPU V0.0\n"
	if m := cpuModel(info); m != "Ingenic XBurst V4.15  FPU V0.0" {
		t.Fatalf("model %q", m)
	}
	if m := cpuModel("processor : 0\n"); m != "" {
		t.Fatalf("model %q", m)
	}
}
