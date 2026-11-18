// SPDX-License-Identifier: GPL-3.0-or-later
package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestProfileOnlyWhenAsked(t *testing.T) {
	d := t.TempDir()
	startProfile(d)
	if _, err := os.Stat(filepath.Join(d, "cpu.pprof")); err == nil {
		t.Fatal("profile written without the profile file")
	}
	if err := os.WriteFile(filepath.Join(d, "profile"), []byte("5\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	startProfile(d)
	profStop()
	defer func() { profStop = func() {} }()
	if st, err := os.Stat(filepath.Join(d, "cpu.pprof")); err != nil || st.Size() == 0 {
		t.Fatalf("no profile: %v", err)
	}
}
