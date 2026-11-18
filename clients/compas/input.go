// SPDX-License-Identifier: GPL-3.0-or-later
// Input: key presses and releases from every input device, and taps on the touch screen.
package main

import (
	"encoding/binary"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"time"
	"unsafe"
)

// Linux input event codes the app uses.
const (
	evKey = 0x01
	evAbs = 0x03

	keyEnter     = 28
	keyVolDown   = 114
	keyVolUp     = 115
	keyPower     = 116
	keyNext      = 163
	keyPlayPause = 164
	keyPrev      = 165
	keyPlay      = 207
	btnTouch     = 0x14a

	absX  = 0x00
	absY  = 0x01
	absMX = 0x35 // ABS_MT_POSITION_X
	absMY = 0x36 // ABS_MT_POSITION_Y
	absMT = 0x39 // ABS_MT_TRACKING_ID: -1 = finger lifted (touch screens without BTN_TOUCH)
)

// inEv is one input event for the UI: a key going down or up, or a tap at screen coordinates.
type inEv struct {
	tap  bool
	drag bool // a finger moving vertically: dy pixels since the last drag event
	dy   int
	code uint16
	down bool
	x, y int
}

// what a key does in menus: move up/down or select
const (
	navNone = iota
	navUp
	navDown
	navSelect
)

func nav(code uint16) int {
	switch code {
	case keyVolUp, keyPrev:
		return navUp
	case keyVolDown, keyNext:
		return navDown
	case keyPlayPause, keyPlay, keyEnter:
		return navSelect
	}
	return navNone
}

// axis is the reported range of one touch axis (scaled onto the screen).
type axis struct{ min, max int }

func (a axis) scale(v, size int) int {
	if a.max <= a.min {
		return v
	}
	return (v - a.min) * size / (a.max - a.min + 1)
}

// eviocgabs is EVIOCGABS(code): _IOR('E', 0x40 + code, struct input_absinfo); MIPS encodes the direction
// in 3 bits at bit 29, other architectures in 2 bits at bit 30.
func eviocgabs(code int) uintptr {
	req := uintptr(24<<16 | 'E'<<8 | (0x40 + code))
	if runtime.GOARCH == "mips" || runtime.GOARCH == "mipsle" {
		return req | 2<<29
	}
	return req | 2<<30
}

func absRange(fd uintptr, code int) axis {
	var info [6]int32 // value, minimum, maximum, fuzz, flat, resolution
	if _, _, e := syscall.Syscall(syscall.SYS_IOCTL, fd, eviocgabs(code), uintptr(unsafe.Pointer(&info[0]))); e != 0 {
		return axis{}
	}
	return axis{int(info[1]), int(info[2])}
}

// devName is EVIOCGNAME: the input device's name.
func devName(fd uintptr) string {
	var b [64]byte
	req := uintptr(len(b)<<16 | 'E'<<8 | 0x06)
	if runtime.GOARCH == "mips" || runtime.GOARCH == "mipsle" {
		req |= 2 << 29
	} else {
		req |= 2 << 30
	}
	if _, _, e := syscall.Syscall(syscall.SYS_IOCTL, fd, req, uintptr(unsafe.Pointer(&b[0]))); e != 0 {
		return "?"
	}
	return strings.TrimRight(string(b[:]), "\x00")
}

// dragStart is how far (px) a finger must move before a touch is a drag (scrolls) instead of a tap.
const dragStart = 12

// inputs delivers key presses/releases (repeats are ignored, like the player does) and touch taps
// (finger lifted) from every input device. `SYNCMUSIC_INPUT` = one file or fifo instead (tests).
func inputs(w, h int) <-chan inEv {
	ch := make(chan inEv, 16)
	paths := []string{os.Getenv("SYNCMUSIC_INPUT")}
	if paths[0] == "" {
		paths, _ = filepath.Glob("/dev/input/event*")
	}
	recSize := 8 + int(unsafe.Sizeof(uintptr(0)))*2 // struct timeval + type, code, value
	for _, p := range paths {
		go func(p string) {
			step("input %s: opening", p)
			f, err := os.Open(p)
			if err != nil {
				step("input %s: %v", p, err)
				return
			}
			defer f.Close()
			ax, ay := absRange(f.Fd(), absMX), absRange(f.Fd(), absMY)
			if ax.max == 0 {
				ax, ay = absRange(f.Fd(), absX), absRange(f.Fd(), absY)
			}
			name := devName(f.Fd())
			if name == "hyn_ts" {
				// The HiBy R1's touch driver advertises a 720x1280 range but reports panel pixels
				// (the Compás player treats it the same way): use the positions as they come.
				ax, ay = axis{}, axis{}
			}
			step("input %s: %s, touch x %v y %v", p, name, ax, ay)
			logged := 0
			x, y, touching := 0, 0, false
			started, dragging, startY, lastY := false, false, 0, 0
			buf := make([]byte, recSize)
			send := func(e inEv) bool {
				select {
				case ch <- e:
					return true
				default:
					return false
				}
			}
			lift := func() {
				touching = false
				if !dragging {
					send(inEv{tap: true, x: x, y: y})
				}
				started, dragging = false, false
			}
			for {
				if _, err := io.ReadFull(f, buf); err != nil {
					return
				}
				ev := buf[recSize-8:]
				typ, code := binary.LittleEndian.Uint16(ev), binary.LittleEndian.Uint16(ev[2:])
				val := int32(binary.LittleEndian.Uint32(ev[4:]))
				if logged < 40 && typ != 0 { // a few raw events per device for diagnosis (.last-session.log)
					logged++
					fmt.Printf("input %s %s: type %d code %d value %d\n", time.Now().Format("15:04:05.000"), filepath.Base(p), typ, code, val)
				}
				switch {
				case typ == evAbs && code == absMT:
					if val >= 0 {
						touching = true
					} else if touching {
						lift()
					}
				case typ == evAbs && (code == absMX || code == absX):
					x = ax.scale(int(val), w)
				case typ == evAbs && (code == absMY || code == absY):
					y = ay.scale(int(val), h)
				case typ == evKey && code == btnTouch:
					if val == 1 {
						touching = true
					} else if val == 0 && touching {
						lift()
					}
				case typ == 0 && touching: // SYN_REPORT: one complete touch frame
					switch {
					case !started:
						started, startY, lastY = true, y, y
					case !dragging && (y-startY > dragStart || startY-y > dragStart):
						dragging = true
						fallthrough
					case dragging && y != lastY:
						if send(inEv{drag: true, dy: y - lastY}) {
							lastY = y
						}
					}
				case typ == evKey && (val == 0 || val == 1):
					send(inEv{code: code, down: val == 1})
				}
			}
		}(p)
	}
	return ch
}
