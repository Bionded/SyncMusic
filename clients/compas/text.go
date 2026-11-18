// SPDX-License-Identifier: GPL-3.0-or-later
// Antialiased text: Inter (SIL OFL 1.1, Rasmus Andersson) pre-rasterised by fontgen/ at the sizes the screens use
// (4-bit alpha), blended onto the RGB565 back buffer.
package main

import (
	"bytes"
	"compress/gzip"
	_ "embed"
	"encoding/binary"
	"errors"
	"io"
	"math"
	"sort"
	"strings"
)

//go:embed inter.atlas.gz
var atlasGz []byte

const (
	wRegular = 0
	wSemi    = 1
	wBold    = 2
)

type glyph struct {
	r         rune
	off       int32 // into face.bits
	adv       int16 // 1/64 px
	left, top int8  // from the pen position; top = pixels above the baseline
	w, h      uint8
}

type face struct {
	size, weight, ascent, height int
	glyphs                       []glyph // sorted by rune
	bits                         []byte
	low                          []int16 // index into glyphs for runes below lowRunes (-1 = none): no search per letter
}

const lowRunes = 0x500 // Latin and Cyrillic

type fonts struct{ faces []*face }

// loadFonts unpacks the atlas into one buffer of its exact size (the player has little memory to spare).
func loadFonts() (*fonts, error) {
	if len(atlasGz) < 4 {
		return nil, errors.New("font atlas: empty")
	}
	z, err := gzip.NewReader(bytes.NewReader(atlasGz))
	if err != nil {
		return nil, err
	}
	b := make([]byte, binary.LittleEndian.Uint32(atlasGz[len(atlasGz)-4:])) // gzip's ISIZE
	if _, err := io.ReadFull(z, b); err != nil {
		return nil, err
	}
	if len(b) < 5 || string(b[:4]) != "SMF1" {
		return nil, errors.New("font atlas: bad header")
	}
	n, p := int(b[4]), 5
	fs := &fonts{}
	for range n {
		f := &face{size: int(b[p]), weight: int(b[p+1]), ascent: int(b[p+2]), height: int(b[p+3])}
		cnt := int(binary.LittleEndian.Uint16(b[p+4:]))
		p += 6
		f.glyphs = make([]glyph, cnt)
		for i := range cnt {
			f.glyphs[i] = glyph{r: rune(binary.LittleEndian.Uint32(b[p:])), adv: int16(binary.LittleEndian.Uint16(b[p+4:])),
				left: int8(b[p+6]), top: int8(b[p+7]), w: b[p+8], h: b[p+9]}
			p += 10
		}
		fs.faces = append(fs.faces, f)
	}
	bits := b[p:]
	off := 0
	for _, f := range fs.faces { // bitmaps are in the atlas order: offsets first, then sort by rune
		f.bits = bits
		for i := range f.glyphs {
			g := &f.glyphs[i]
			g.off = int32(off)
			off += (int(g.w) + 1) / 2 * int(g.h)
		}
		sort.Slice(f.glyphs, func(i, j int) bool { return f.glyphs[i].r < f.glyphs[j].r })
	}
	if off > len(bits) {
		return nil, errors.New("font atlas: truncated")
	}
	return fs, nil
}

// face returns the face of that size and weight (the nearest size of the weight when it is not in the atlas).
func (fs *fonts) face(size, weight int) *face {
	var best *face
	for _, f := range fs.faces {
		if f.weight != weight {
			continue
		}
		if best == nil || abs(f.size-size) < abs(best.size-size) {
			best = f
		}
	}
	if best == nil {
		return fs.faces[0]
	}
	return best
}

func abs(v int) int {
	if v < 0 {
		return -v
	}
	return v
}

func (f *face) find(r rune) *glyph {
	if r >= 0 && r < lowRunes {
		if f.low == nil {
			f.low = make([]int16, lowRunes)
			for i := range f.low {
				f.low[i] = -1
			}
			for i, g := range f.glyphs {
				if g.r >= 0 && g.r < lowRunes {
					f.low[g.r] = int16(i)
				}
			}
		}
		if i := f.low[r]; i >= 0 {
			return &f.glyphs[i]
		}
		return nil
	}
	i := sort.Search(len(f.glyphs), func(i int) bool { return f.glyphs[i].r >= r })
	if i < len(f.glyphs) && f.glyphs[i].r == r {
		return &f.glyphs[i]
	}
	return nil
}

func (f *face) glyph(r rune) *glyph {
	if g := f.find(r); g != nil {
		return g
	}
	return f.find('?')
}

// width of s in pixels, with `track` extra pixels after every letter (letter spacing).
func (f *face) width(s string, track int) int {
	w := 0
	for _, r := range s {
		if r == '⚡' && f.find(r) == nil {
			w += int(float64(f.ascent)*0.85*0.62*64) + track*64
			continue
		}
		w += int(f.glyph(r).adv) + track*64
	}
	return (w + 32) / 64
}

// fit cuts s with "…" so that it is at most maxW pixels wide.
func (f *face) fit(s string, maxW, track int) string {
	if f.width(s, track) <= maxW {
		return s
	}
	rs := []rune(s)
	for len(rs) > 0 && f.width(string(rs)+"…", track) > maxW {
		rs = rs[:len(rs)-1]
	}
	return strings.TrimRight(string(rs), " ") + "…"
}

// blend mixes colour c over the pixel at i with alpha a (0–15).
func (s *screen) blend(i int, c uint16, a int) {
	if a >= 15 {
		s.pix[i] = c
		return
	}
	d := s.pix[i]
	mix := func(fg, bg uint16) uint16 { return uint16(((int(fg)*a+int(bg)*(15-a))*4369 + 32768) >> 16) } // x/15 without a division
	r := mix(c>>11, d>>11)
	g := mix(c>>5&63, d>>5&63)
	b := mix(c&31, d&31)
	s.pix[i] = r<<11 | g<<5 | b
}

// draw writes s with its top-left corner at (x, y) (y = top of the line box); returns the width drawn.
func (s *screen) draw(f *face, x, y int, str string, c uint16, track int) int {
	pen := x * 64
	base := y + f.ascent
	for _, r := range str {
		if r == '⚡' && f.find(r) == nil { // not in Inter: a small bolt
			px, top := float64(pen)/64, float64(base)-float64(f.ascent)*0.75
			hh := float64(f.ascent) * 0.85
			lw := math.Max(1.3, hh/9)
			s.stroke(px+hh*0.42, top, px+hh*0.12, top+hh*0.55, lw, c)
			s.stroke(px+hh*0.12, top+hh*0.55, px+hh*0.45, top+hh*0.45, lw, c)
			s.stroke(px+hh*0.45, top+hh*0.45, px+hh*0.18, top+hh, lw, c)
			pen += int(hh*0.62*64) + track*64
			continue
		}
		g := f.glyph(r)
		gx, gy, stride := (pen+32)/64+int(g.left), base-int(g.top), (int(g.w)+1)/2
		for j := 0; j < int(g.h); j++ {
			py := gy + j
			if py < 0 || py >= s.h {
				continue
			}
			row := f.bits[int(g.off)+j*stride:]
			for i := 0; i < int(g.w); i++ {
				px := gx + i
				if px < 0 || px >= s.w {
					continue
				}
				a := int(row[i/2] >> (4 * (i % 2)) & 15)
				if a > 0 {
					s.blend(py*s.w+px, c, a)
				}
			}
		}
		pen += int(g.adv) + track*64
	}
	return (pen+32)/64 - x
}

// wrapPx breaks text into lines at most w pixels wide (by words; a long word is cut), at most n lines.
type wrapKey struct {
	f    *face
	text string
	w, n int
}

// wrapCache: the lines of texts wrapped before (a page is laid out again on every frame)
var wrapCache = map[wrapKey][]string{}

func wrapPx(f *face, text string, w, n int) []string {
	k := wrapKey{f, text, w, n}
	if l, ok := wrapCache[k]; ok {
		return l
	}
	l := wrapPxNew(f, text, w, n)
	if len(wrapCache) > 512 { // bounded
		clear(wrapCache)
	}
	wrapCache[k] = l
	return l
}

func wrapPxNew(f *face, text string, w, n int) []string {
	var lines []string
	cur := ""
	for _, word := range strings.Fields(text) {
		for f.width(word, 0) > w {
			if cur != "" {
				lines, cur = append(lines, cur), ""
			}
			rs := []rune(word)
			k := len(rs) - 1
			for k > 1 && f.width(string(rs[:k]), 0) > w {
				k--
			}
			lines, word = append(lines, string(rs[:k])), string(rs[k:])
		}
		switch {
		case cur == "":
			cur = word
		case f.width(cur+" "+word, 0) <= w:
			cur += " " + word
		default:
			lines, cur = append(lines, cur), word
		}
	}
	if cur != "" {
		lines = append(lines, cur)
	}
	if len(lines) > n {
		lines = lines[:n]
		lines[n-1] = f.fit(lines[n-1]+"…", w, 0)
	}
	return lines
}
