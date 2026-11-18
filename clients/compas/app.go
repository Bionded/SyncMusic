// Framebuffer drawing for the full-screen UI (RGB565); the screens themselves are in screens.go.
//
// Pairing: there is no keyboard on the R1, so the user writes "<server> <code>" into
// $COMPAS_APP_DATA/pair.txt in USB storage mode; the app pairs on its next start and removes the file.
//
// Test hooks (host/qemu): SYNCMUSIC_FB=<file> draws into a plain file instead of /dev/fb0
// (size from COMPAS_SCREEN_WIDTH/HEIGHT, RGB565), SYNCMUSIC_INPUT=<file or fifo> reads input_event
// records from there instead of /dev/input/event*.
package main

import (
	"bytes"
	_ "embed"
	"encoding/binary"
	"fmt"
	"os"
	"strconv"
	"syscall"
	"unsafe"
)

type screen struct {
	w, h    int
	pix     []uint16 // back buffer
	out     []byte   // framebuffer memory (visible page) or file contents
	stride  int      // bytes per framebuffer line
	file    *os.File // test mode: write out to this file
	font    *fonts
	swiped  bool // the user scrolled by a swipe: scroll is the content offset, else the selection stays visible
	scroll  int
	off     int             // the content offset of the last drawn page
	snaps   map[[5]int]snap // logo and icons drawn before (see cached)
	shown   []uint16        // what the last flush put on the screen: unchanged rows are not written again
	tall    []uint16        // the whole scrolling content of the last page (see drawPage)
	tallKey string          // what tall shows
	pageKey string          // frameKey(page, sel) of the page about to be drawn, "" = unknown
}

func rgb(r, g, b uint8) uint16 { return uint16(r>>3)<<11 | uint16(g>>2)<<5 | uint16(b>>3) }

var (
	colBg     = rgb(0x0f, 0x11, 0x15)
	colCard   = rgb(0x18, 0x1b, 0x21)
	colTile   = rgb(0x21, 0x25, 0x2d) // a tile inside a card
	colText   = rgb(0xe8, 0xea, 0xed)
	colSoft   = rgb(0xc3, 0xc7, 0xcf)
	colDim    = rgb(0x9a, 0xa0, 0xa6)
	colAcc    = rgb(0x2d, 0xd4, 0xbf)
	colAccDim = rgb(0x1a, 0x6e, 0x64)
	colOnAcc  = rgb(0x0f, 0x11, 0x15)
	colErr    = rgb(0xf8, 0x71, 0x71)
	colWarn   = rgb(0xf5, 0x9e, 0x0b)
	colWarnBg = rgb(0x2a, 0x21, 0x10)
	colTrack  = rgb(0x2a, 0x2e, 0x36)
)

func envInt(name string, def int) int {
	if v, err := strconv.Atoi(os.Getenv(name)); err == nil && v > 0 {
		return v
	}
	return def
}

func openScreen(f *fonts) (*screen, error) {
	if p := os.Getenv("SYNCMUSIC_FB"); p != "" {
		w, h := envInt("COMPAS_SCREEN_WIDTH", 480), envInt("COMPAS_SCREEN_HEIGHT", 800)
		fd, err := os.OpenFile(p, os.O_CREATE|os.O_RDWR|os.O_TRUNC, 0o644)
		if err != nil {
			return nil, err
		}
		return &screen{w: w, h: h, pix: make([]uint16, w*h), out: make([]byte, w*h*2), stride: w * 2, file: fd, font: f}, nil
	}
	fd, err := syscall.Open("/dev/fb0", syscall.O_RDWR, 0)
	if err != nil {
		return nil, err
	}
	var vinfo [160]byte                                                  // struct fb_var_screeninfo: all __u32
	var finfo [128]byte                                                  // struct fb_fix_screeninfo
	if err := ioctl(fd, 0x4600, unsafe.Pointer(&vinfo[0])); err != nil { // FBIOGET_VSCREENINFO
		return nil, fmt.Errorf("FBIOGET_VSCREENINFO: %w", err)
	}
	if err := ioctl(fd, 0x4602, unsafe.Pointer(&finfo[0])); err != nil { // FBIOGET_FSCREENINFO
		return nil, fmt.Errorf("FBIOGET_FSCREENINFO: %w", err)
	}
	u := func(b []byte, i int) int { return int(binary.LittleEndian.Uint32(b[i*4:])) }
	xres, yres, xoff, yoff, bpp := u(vinfo[:], 0), u(vinfo[:], 1), u(vinfo[:], 4), u(vinfo[:], 5), u(vinfo[:], 6)
	if bpp != 16 {
		return nil, fmt.Errorf("framebuffer is %d bpp, only RGB565 is supported", bpp)
	}
	// fb_fix_screeninfo: id[16], unsigned long smem_start, __u32 smem_len, type, type_aux, visual,
	// __u16 xpanstep, ypanstep, ywrapstep, (pad), __u32 line_length
	ul := int(unsafe.Sizeof(uintptr(0)))
	smemLen := int(binary.LittleEndian.Uint32(finfo[16+ul:]))
	lineLen := int(binary.LittleEndian.Uint32(finfo[16+ul+4*4+3*2+2:]))
	mem, err := syscall.Mmap(fd, 0, smemLen, syscall.PROT_READ|syscall.PROT_WRITE, syscall.MAP_SHARED)
	if err != nil {
		return nil, fmt.Errorf("mmap /dev/fb0: %w", err)
	}
	start := yoff*lineLen + xoff*2 // draw into the page the player left visible
	if start+yres*lineLen > len(mem) {
		start = 0
	}
	return &screen{w: xres, h: yres, pix: make([]uint16, xres*yres), out: mem[start:], stride: lineLen, font: f}, nil
}

func ioctl(fd int, req uintptr, arg unsafe.Pointer) error {
	if _, _, e := syscall.Syscall(syscall.SYS_IOCTL, uintptr(fd), req, uintptr(arg)); e != 0 {
		return e
	}
	return nil
}

func (s *screen) fill(x, y, w, h int, c uint16) {
	x0, x1, y0, y1 := max(x, 0), min(x+w, s.w), max(y, 0), min(y+h, s.h)
	if x0 >= x1 || y0 >= y1 {
		return
	}
	first := s.pix[y0*s.w+x0 : y0*s.w+x1]
	first[0] = c
	for n := 1; n < len(first); n *= 2 { // doubling copies (memmove) instead of a store per pixel
		copy(first[n:], first[:n])
	}
	for j := y0 + 1; j < y1; j++ {
		copy(s.pix[j*s.w+x0:j*s.w+x1], first)
	}
}

// le: the CPU stores uint16 little-endian (MIPS LE, x86, arm), so a row can be copied to the framebuffer as bytes.
var le = func() bool { v := uint16(1); return *(*byte)(unsafe.Pointer(&v)) == 1 }()

func u16bytes(v []uint16) []byte {
	return unsafe.Slice((*byte)(unsafe.Pointer(unsafe.SliceData(v))), 2*len(v))
}

// flush copies the changed rows of the back buffer to the screen; an unchanged frame costs one compare.
func (s *screen) flush() {
	if len(s.shown) != len(s.pix) {
		s.shown = make([]uint16, len(s.pix))
		for i := range s.shown {
			s.shown[i] = ^s.pix[i] // differs everywhere: the first frame is written whole
		}
	}
	first, last := -1, -1
	for y := 0; y < s.h; y++ {
		src, old := s.pix[y*s.w:(y+1)*s.w], s.shown[y*s.w:(y+1)*s.w]
		if bytes.Equal(u16bytes(src), u16bytes(old)) { // memequal: much faster than an element loop

			continue
		}
		copy(old, src)
		row := s.out[y*s.stride : y*s.stride+2*s.w]
		if le {
			copy(row, u16bytes(src))
		} else {
			for x, c := range src {
				binary.LittleEndian.PutUint16(row[x*2:], c)
			}
		}
		if first < 0 {
			first = y
		}
		last = y
	}
	if s.file != nil && first >= 0 {
		_, _ = s.file.WriteAt(s.out[first*s.stride:(last+1)*s.stride], int64(first*s.stride))
	}
}

// summary: what a sync did, in both directions.
func summary(r Result) string {
	msg := fmt.Sprintf("Received: %d new, %d removed.", r.Fetched, r.Deleted)
	if r.Failed > 0 {
		msg += fmt.Sprintf(" %d not done (next sync continues).", r.Failed)
	}
	msg += fmt.Sprintf(" Sent: %s, %s, %s.", Count(r.Sent["play"], "play", "plays"),
		Count(r.Sent["favourite"], "heart", "hearts"), Count(r.Sent["position"], "position", "positions"))
	return msg
}
