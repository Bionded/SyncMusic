// SPDX-License-Identifier: GPL-3.0-or-later
// The page renderer: lays a page out like the screen designs (480x800, sizes in pixels): a header (logo, or a back
// key and the title), the content blocks in cards, the settings rows, the key grid, and the main buttons at the
// bottom. Content that does not fit scrolls with the selection.
package main

import (
	"math"
	"strings"
)

var (
	colBorder  = rgb(0x2f, 0x34, 0x3d)
	colKey     = rgb(0x23, 0x27, 0x2e) // keys of a key grid, row separators
	colTeal2   = rgb(0x5e, 0xea, 0xd4) // "Online", a code to compare
	colOther   = rgb(0x6b, 0x72, 0x80) // the other files' part of the card bar
	colWarnTxt = rgb(0xfb, 0xbf, 0x24)
	colWarnSub = rgb(0xe8, 0xd5, 0xb5)
	colWarnBd  = rgb(0xb4, 0x53, 0x09)
	colWarnBox = rgb(0x2a, 0x1f, 0x12)
	colNoteBg  = rgb(0x12, 0x26, 0x2a)
	colNoteBd  = rgb(0x0f, 0x76, 0x6e)
	colNoteTxt = rgb(0xcc, 0xfb, 0xf1)
	colDangBg  = rgb(0x1c, 0x12, 0x14)
	colDangBd  = rgb(0x7f, 0x1d, 0x1d)
	colDangTxt = rgb(0xfc, 0xa5, 0xa5)
)

const (
	uiPad     = 24 // screen margin
	uiCardPad = 18
	uiGap     = 18 // between blocks
)

// noBlit draws scrolling content straight onto the screen (tests compare both ways)
var noBlit bool

// ---- primitives --------------------------------------------------------------------------------------------

// cover is the share of pixel (px, py) inside a rounded rectangle, 0–15.
func roundCover(px, py, x, y, w, h int, r float64) int {
	fx, fy := float64(px)+0.5, float64(py)+0.5
	cx := math.Max(float64(x)+r, math.Min(fx, float64(x+w)-r))
	cy := math.Max(float64(y)+r, math.Min(fy, float64(y+h)-r))
	d := math.Hypot(fx-cx, fy-cy)
	c := r - d + 0.5
	switch {
	case c >= 1:
		return 15
	case c <= 0:
		return 0
	}
	return int(c*15 + 0.5)
}

// cornerTab caches the coverage of an r x r top-left corner (row-major) per radius: corners are drawn with
// integer work only (the player has no FPU, every float operation is emulated).
var cornerTab = map[int][]uint8{}

func corner(r int) []uint8 {
	if t, ok := cornerTab[r]; ok {
		return t
	}
	t := make([]uint8, r*r)
	for j := range r {
		for i := range r {
			t[j*r+i] = uint8(roundCover(i, j, 0, 0, 2*r, 2*r, float64(r)))
		}
	}
	cornerTab[r] = t
	return t
}

// fillRound fills a rectangle with antialiased corners of radius r.
func (s *screen) fillRound(x, y, w, h, r int, c uint16) {
	r = max(0, min(r, w/2, h/2))
	if r == 0 {
		s.fill(x, y, w, h, c)
		return
	}
	s.fill(x, y+r, w, h-2*r, c)
	s.fill(x+r, y, w-2*r, r, c)
	s.fill(x+r, y+h-r, w-2*r, r, c)
	t := corner(r)
	for j := range r {
		for i := range r {
			a := int(t[j*r+i])
			if a == 0 {
				continue
			}
			for _, p := range [4][2]int{{x + i, y + j}, {x + w - 1 - i, y + j}, {x + i, y + h - 1 - j}, {x + w - 1 - i, y + h - 1 - j}} {
				if p[0] >= 0 && p[0] < s.w && p[1] >= 0 && p[1] < s.h {
					s.blend(p[1]*s.w+p[0], c, a)
				}
			}
		}
	}
}

// box: a rounded rectangle with a border of width bw (0 = none).
func (s *screen) box(x, y, w, h, r int, bg, border uint16, bw int) {
	if bw > 0 {
		s.fillRound(x, y, w, h, r, border)
		s.fillRound(x+bw, y+bw, w-2*bw, h-2*bw, r-bw, bg)
		return
	}
	s.fillRound(x, y, w, h, r, bg)
}

// stroke draws a line of width lw from (x0, y0) to (x1, y1) with round ends (antialiased).
func (s *screen) stroke(x0, y0, x1, y1, lw float64, c uint16) {
	s.shape(math.Min(x0, x1)-lw, math.Min(y0, y1)-lw, math.Max(x0, x1)+lw, math.Max(y0, y1)+lw, lw, c,
		func(px, py float64) float64 {
			dx, dy := x1-x0, y1-y0
			t := 0.0
			if l := dx*dx + dy*dy; l > 0 {
				t = math.Max(0, math.Min(1, ((px-x0)*dx+(py-y0)*dy)/l))
			}
			return math.Hypot(px-x0-t*dx, py-y0-t*dy)
		})
}

// arc draws a circle arc of radius r around (cx, cy) from angle a0 to a1 (radians, clockwise from 3 o'clock on
// the screen), width lw.
func (s *screen) arc(cx, cy, r, a0, a1, lw float64, c uint16) {
	s.shape(cx-r-lw, cy-r-lw, cx+r+lw, cy+r+lw, lw, c, func(px, py float64) float64 {
		a := math.Atan2(py-cy, px-cx)
		for a < a0 {
			a += 2 * math.Pi
		}
		if a <= a1 {
			return math.Abs(math.Hypot(px-cx, py-cy) - r)
		}
		e0x, e0y := cx+r*math.Cos(a0), cy+r*math.Sin(a0)
		e1x, e1y := cx+r*math.Cos(a1), cy+r*math.Sin(a1)
		return math.Min(math.Hypot(px-e0x, py-e0y), math.Hypot(px-e1x, py-e1y))
	})
}

// shape fills every pixel of the box whose centre is within lw/2 of the shape (dist = distance to it).
func (s *screen) shape(x0, y0, x1, y1, lw float64, c uint16, dist func(px, py float64) float64) {
	for j := max(int(y0), 0); j <= min(int(y1)+1, s.h-1); j++ {
		for i := max(int(x0), 0); i <= min(int(x1)+1, s.w-1); i++ {
			cv := lw/2 - dist(float64(i)+0.5, float64(j)+0.5) + 0.5
			if cv > 0 {
				s.blend(j*s.w+i, c, int(math.Min(cv, 1)*15+0.5))
			}
		}
	}
}

func (s *screen) disc(cx, cy, r float64, c uint16) {
	s.shape(cx-r, cy-r, cx+r, cy+r, 2*r, c, func(px, py float64) float64 { return math.Hypot(px-cx, py-cy) })
}

// snap is a drawn picture: its pixels after drawing (the key holds the hash of the background before).
// snap kinds besides the icons
const (
	snapToggleOn uint8 = 100 + iota
	snapToggleOff
	snapChevron
	snapBack
	snapListChevron
)

type snap struct{ pix []uint16 }

// cached draws the vector picture `draw` (kind: 0 = logo, else an icon) of size x size at (x, y) once and later
// copies its pixels when the background under it is the same: the vector shapes need float maths, which the
// player emulates in software.
func (s *screen) cached(kind uint8, x, y, size int, draw func()) {
	s.cachedRect(kind, x, y, size, size, draw)
}

// cachedRect is cached for a w x h rectangle.
func (s *screen) cachedRect(kind uint8, x, y, w, h int, draw func()) {
	if x < 0 || y < 0 || x+w > s.w || y+h > s.h {
		draw()
		return
	}
	hv := uint32(2166136261)
	for j := y; j < y+h; j++ {
		for _, v := range s.pix[j*s.w+x : j*s.w+x+w] {
			hv = (hv ^ uint32(v)) * 16777619
		}
	}
	// keyed by picture and background, not by position: a scrolled row finds its icon again
	key := [5]int{int(kind), w, h, int(hv)}
	if c, ok := s.snaps[key]; ok {
		for j := range h {
			copy(s.pix[(y+j)*s.w+x:(y+j)*s.w+x+w], c.pix[j*w:])
		}
		return
	}
	draw()
	c := snap{pix: make([]uint16, w*h)}
	for j := range h {
		copy(c.pix[j*w:], s.pix[(y+j)*s.w+x:(y+j)*s.w+x+w])
	}
	if s.snaps == nil || len(s.snaps) > 256 { // bounded: about 40 kB at most
		s.snaps = map[[5]int]snap{}
	}
	s.snaps[key] = c
}

// icons, drawn as vectors
const (
	iconNone uint8 = iota
	iconCheck
	iconWifiOff
	iconError
)

// logo: the app mark, a teal rounded square with two sync arrows (size px).
func (s *screen) logo(x, y, size int) {
	s.fillRound(x, y, size, size, size*3/10, colAcc)
	k := float64(size) / 40 * 22 / 24 // the 24-unit icon drawn 22 px wide in a 40 px square
	ox, oy := float64(x)+(float64(size)-24*k)/2, float64(y)+(float64(size)-24*k)/2
	p := func(u, v float64) (float64, float64) { return ox + u*k, oy + v*k }
	lw := 2.2 * k
	cx, cy := p(12, 12)
	s.arc(cx, cy, 9*k, 0, 0.75*math.Pi-0.08, lw, colOnAcc)       // M21 12 a9 9 → bottom left
	s.arc(cx, cy, 9*k, math.Pi, 1.75*math.Pi-0.08, lw, colOnAcc) // M3 12 a9 9 → top right
	ax, ay := p(18, 2)
	bx, by := p(18, 6)
	cx2, cy2 := p(14, 6)
	s.stroke(ax, ay, bx, by, lw, colOnAcc)
	s.stroke(bx, by, cx2, cy2, lw, colOnAcc)
	ax, ay = p(6, 22)
	bx, by = p(6, 18)
	cx2, cy2 = p(10, 18)
	s.stroke(ax, ay, bx, by, lw, colOnAcc)
	s.stroke(bx, by, cx2, cy2, lw, colOnAcc)
}

func (s *screen) icon(kind uint8, x, y, size int) {
	f := float64(size)
	X, Y := float64(x), float64(y)
	switch kind {
	case iconCheck, iconError:
		col := colAcc
		if kind == iconError {
			col = colErr
		}
		s.arc(X+f/2, Y+f/2, f/2-1.5, -math.Pi, math.Pi, 2.6, col)
		if kind == iconCheck {
			s.stroke(X+f*0.30, Y+f*0.52, X+f*0.45, Y+f*0.66, 2.6, col)
			s.stroke(X+f*0.45, Y+f*0.66, X+f*0.71, Y+f*0.38, 2.6, col)
		} else {
			s.stroke(X+f*0.5, Y+f*0.28, X+f*0.5, Y+f*0.56, 2.6, col)
			s.disc(X+f*0.5, Y+f*0.72, 1.8, col)
		}
	case iconWifiOff:
		lw := f / 64 * 5.5
		cx, cy := X+f/2, Y+f*0.82
		for _, r := range []float64{0.62, 0.42, 0.22} {
			s.arc(cx, cy, f*r, -0.78*math.Pi, -0.22*math.Pi, lw, colWarn)
		}
		s.disc(cx, cy, f*0.09, colWarn)
		s.stroke(X+f*0.12, Y+f*0.06, X+f*0.88, Y+f*0.94, lw, colWarn)
	}
}

// ---- layout ------------------------------------------------------------------------------------------------

// text styles: size → face, colour, letter spacing, upper case
const (
	sizeBody    = 0 // 15 px
	sizeHead    = 2 // 20 px semibold (a file name, a server name)
	sizeCode    = 3 // 32 px bold, spaced (a code to compare)
	sizeLabel   = 4 // 13 px upper case, spaced, grey (a card's title)
	sizeSmall   = 5 // 14 px
	sizeHint    = 6 // 13 px
	sizeBig     = 7 // 32 px bold (74 / 128)
	sizeRow     = 8 // 17 px (a label / value row)
	sizeRowSemi = 9 // 17 px semibold (a box's title)
)

func (s *screen) style(size int) (*face, int, bool) {
	fs := s.font
	switch size {
	case sizeHead:
		return fs.face(20, wSemi), 0, false
	case sizeCode:
		return fs.face(32, wBold), 6, false
	case sizeLabel:
		return fs.face(13, wRegular), 1, true
	case sizeSmall:
		return fs.face(14, wRegular), 0, false
	case sizeHint:
		return fs.face(13, wRegular), 0, false
	case sizeBig:
		return fs.face(32, wBold), 0, false
	case sizeRow:
		return fs.face(17, wRegular), 0, false
	case sizeRowSemi:
		return fs.face(17, wSemi), 0, false
	}
	return fs.face(15, wRegular), 0, false
}

func lineCol(l line, def uint16) uint16 {
	if l.col != 0 {
		return l.col
	}
	return def
}

// blockH is the height of one block at width w.
func (s *screen) blockH(l line, w int) int {
	f15 := s.font.face(15, wRegular)
	f13 := s.font.face(13, wRegular)
	switch {
	case len(l.stats) > 0:
		rows := (len(l.stats) + 2) / 3
		return rows*(f13.height+2+f15.height) + (rows-1)*10
	case len(l.grid) > 0:
		f, _, _ := s.style(l.size)
		rows := (len(l.grid) + 1) / 2
		return rows*f.height + (rows-1)*10
	case len(l.bar) >= 2:
		return 10
	case len(l.steps) == 2:
		if len(l.names) > 0 {
			return 6 + 12 + f13.height
		}
		return 6
	case l.check != nil:
		return 44
	}
	f, track, up := s.style(l.size)
	t := l.text
	if up {
		t = strings.ToUpper(t)
	}
	if l.right != "" {
		return f.height
	}
	return max(1, len(wrapPx(f, t, w-track*len([]rune(t))/max(1, len([]rune(t))), 8))) * f.height
}

// drawBlock draws one block at (x, y), width w.
func (s *screen) drawBlock(l line, x, y, w int) {
	f15 := s.font.face(15, wRegular)
	f13 := s.font.face(13, wRegular)
	switch {
	case len(l.stats) > 0:
		n := min(len(l.stats), 3)
		cw := w / n
		for i, st := range l.stats {
			tx, ty := x+i%3*cw, y+i/3*(f13.height+2+f15.height+10)
			s.draw(f13, tx, ty, f13.fit(st.label, cw-8, 0), colDim, 0)
			s.draw(f15, tx, ty+f13.height+2, f15.fit(st.value, cw-8, 0), colText, 0)
		}
		return
	case len(l.grid) > 0:
		f, _, _ := s.style(l.size)
		cw := (w - 16) / 2
		for i, t := range l.grid {
			tx, ty := x+i%2*(cw+16), y+i/2*(f.height+10)
			s.draw(f, tx, ty, f.fit(t, cw, 0), lineCol(l, colText), 0)
		}
		return
	case len(l.bar) >= 2:
		s.fillRound(x, y, w, 10, 5, colTrack)
		if l.bar[1] > 0 {
			f := int(int64(w) * min(l.bar[0], l.bar[1]) / l.bar[1])
			if len(l.bar) == 3 { // the other files' part after ours
				o := int(int64(w) * min(l.bar[2], l.bar[1]) / l.bar[1])
				s.fill(x+f, y, min(o, w-f), 10, colOther)
				s.fillRound(x, y, w, 10, 5, colTrack) // keep the rounded ends: redraw both on a fresh track
				s.fill(x+5, y, max(0, f+o-5), 10, colOther)
				s.fill(x+5, y, max(0, f-5), 10, colAcc)
				s.fillRound(x, y, min(10, max(f, 0)), 10, 5, colAcc)
				return
			}
			if f > 0 {
				s.fillRound(x, y, max(f, 10), 10, 5, colAcc)
			}
		}
		return
	case len(l.steps) == 2:
		n := max(l.steps[1], 1)
		sw := (w - (n-1)*6) / n
		for i := 0; i < n; i++ {
			c := colTrack
			if i+1 < l.steps[0] {
				c = colAcc
			} else if i+1 == l.steps[0] {
				c = colAccDim
			}
			s.fillRound(x+i*(sw+6), y, sw, 6, 3, c)
		}
		if len(l.names) > 0 {
			fs := s.font.face(13, wSemi)
			for i, name := range l.names {
				f, c := f13, colDim
				if i+1 == l.steps[0] {
					f, c = fs, colText
				}
				name = f.fit(name, sw+4, 0)
				tw := f.width(name, 0)
				tx := x + i*(sw+6) + (sw-tw)/2
				if i == 0 {
					tx = x
				} else if i == n-1 {
					tx = x + w - tw
				}
				s.draw(f, tx, y+18, name, c, 0)
			}
		}
		return
	case l.check != nil:
		s.box(x+3, y+10, 24, 24, 4, colText, colText, 0)
		if *l.check {
			s.stroke(float64(x)+9, float64(y)+22, float64(x)+13.5, float64(y)+27, 2.4, colBg)
			s.stroke(float64(x)+13.5, float64(y)+27, float64(x)+21, float64(y)+17, 2.4, colBg)
		}
		f := s.font.face(16, wRegular)
		s.draw(f, x+40, y+(44-f.height)/2, f.fit(l.text, w-40, 0), lineCol(l, colText), 0)
		return
	}
	f, track, up := s.style(l.size)
	t := l.text
	if up {
		t = strings.ToUpper(t)
	}
	col := lineCol(l, colText)
	if l.right != "" {
		rf, rt := f, track
		if l.size == sizeRow || l.size == sizeBody && !up {
			rf = s.font.face(f.size, wSemi)
		}
		if l.size == sizeBig {
			rf, rt = s.font.face(15, wRegular), 0
		}
		rcol := l.rcol
		if rcol == 0 {
			rcol = col
		}
		r := l.right
		if up {
			r = strings.ToUpper(r)
		}
		rw := rf.width(r, rt)
		ry := y
		if l.size == sizeBig {
			ry = y + f.ascent - rf.ascent
		}
		s.draw(rf, x+w-rw, ry, r, rcol, rt)
		s.draw(f, x, y, f.fit(t, w-rw-12, track), col, track)
		return
	}
	for _, ln := range wrapPx(f, t, w, 8) {
		if l.dot != 0 {
			s.disc(float64(x)+4, float64(y)+float64(f.height)/2, 4, l.dot)
			s.draw(f, x+16, y, ln, col, track)
		} else {
			s.draw(f, x, y, ln, col, track)
		}
		y += f.height
	}
}

// group: a run of blocks with the same box, its height at width W.
func (s *screen) runH(run []line, W int) int {
	w := W
	if run[0].box != boxNone {
		w = W - 2*uiCardPad
	}
	h := 0
	for k, l := range run {
		if k > 0 {
			h += blockGap(run[k-1], l)
		}
		h += s.blockH(l, w)
	}
	if run[0].box != boxNone {
		h += 2 * uiCardPad
		if run[0].box == boxWarn || run[0].box == boxNote {
			h -= 2 * 2
		}
	}
	return h
}

// blockGap: the space between two blocks inside one run.
func blockGap(a, b line) int {
	switch {
	case len(b.bar) >= 2 || len(a.bar) >= 2 || len(b.stats) > 0 || len(b.grid) > 0 || len(a.grid) > 0:
		return 10
	case a.size == sizeRow && b.size == sizeRow:
		return 14
	}
	return 6
}

func (s *screen) drawRun(run []line, x, y, W int) {
	box := run[0].box
	w := W
	h := s.runH(run, W)
	pad := 0
	switch box {
	case boxCard, boxCard2:
		s.fillRound(x, y, W, h, 16, colCard)
		pad = uiCardPad
	case boxWarn:
		s.box(x, y, W, h, 16, colWarnBox, colWarnBd, 1)
		pad = uiCardPad - 2
	case boxNote:
		s.box(x, y, W, h, 16, colNoteBg, colNoteBd, 1)
		pad = uiCardPad - 2
	}
	x, y, w = x+pad, y+pad, w-2*pad
	for k, l := range run {
		if k > 0 {
			y += blockGap(run[k-1], l)
		}
		s.drawBlock(l, x, y, w)
		y += s.blockH(l, w)
	}
}

// button looks
func (s *screen) drawButton(r rect, b button, selected bool) {
	bg, fg, bd := colCard, colText, colBorder
	f := s.font.face(17, wRegular)
	rad := 14
	switch b.kind {
	case btnPrimary:
		bg, fg, bd = colAcc, colOnAcc, colAcc
		f, rad = s.font.face(22, wBold), 18
		if r.h < 64 {
			f, rad = s.font.face(17, wBold), 14
		}
	case btnDanger:
		bg, fg, bd = colDangBg, colDangTxt, colDangBd
		f = s.font.face(17, wSemi)
	case btnBack:
		rad = 12
	}
	ring := colAcc
	if b.kind == btnPrimary {
		ring = colText
	}
	if selected {
		s.box(r.x, r.y, r.w, r.h, rad, bg, ring, 2)
	} else if b.kind == btnPrimary || b.kind == btnBack {
		s.fillRound(r.x, r.y, r.w, r.h, rad, bg)
	} else {
		s.box(r.x, r.y, r.w, r.h, rad, bg, bd, 1)
	}
	if b.kind == btnBack {
		cx, cy := float64(r.x)+float64(r.w)/2, float64(r.y)+float64(r.h)/2
		s.cachedRect(snapBack, int(cx)-6, int(cy)-9, 14, 19, func() {
			s.stroke(cx+2.5, cy-5.5, cx-2.5, cy, 2, colText)
			s.stroke(cx-2.5, cy, cx+2.5, cy+5.5, 2, colText)
		})
		return
	}
	label := f.fit(b.label, r.w-24, 0)
	tw := f.width(label, 0)
	s.draw(f, r.x+(r.w-tw)/2, r.y+(r.h-f.height)/2, label, fg, 0)
}

// listRow draws a list item of a card: a name, a grey second line and a chevron.
func (s *screen) listRow(r rect, b button, selected bool) {
	if selected {
		s.box(r.x+4, r.y+3, r.w-8, r.h-6, 10, colKey, colAcc, 2)
	}
	sub := strings.SplitN(b.label, "\n", 2)
	fn, fd := s.font.face(18, wSemi), s.font.face(14, wRegular)
	ty := r.y + (r.h-fn.height-fd.height-2)/2
	s.draw(fn, r.x+uiCardPad, ty, fn.fit(sub[0], r.w-60, 0), colText, 0)
	s.draw(fd, r.x+uiCardPad, ty+fn.height+2, fd.fit(sub[1], r.w-60, 0), colSoft, 0)
	cx, cy := float64(r.x+r.w-22), float64(r.y)+float64(r.h)/2
	s.cachedRect(snapListChevron, int(cx)-6, int(cy)-9, 14, 19, func() {
		s.stroke(cx-2.5, cy-5, cx+2, cy, 1.8, colDim)
		s.stroke(cx+2, cy, cx-2.5, cy+5, 1.8, colDim)
	})
}

// settingRow draws one row of a settings card: name left, value right (teal) or a switch for yes/no.
func (s *screen) settingRow(r rect, b button, selected bool) {
	f := s.font.face(17, wRegular)
	name, val := b.label, ""
	if k := strings.LastIndex(b.label, ": "); k > 0 {
		name, val = b.label[:k], b.label[k+2:]
	}
	if selected {
		s.box(r.x+4, r.y+3, r.w-8, r.h-6, 10, colKey, colAcc, 2)
	}
	fg := colText
	if b.kind == btnDanger {
		fg = colDangTxt
	}
	ty := r.y + (r.h-f.height)/2
	right := r.x + r.w - uiCardPad
	switch val {
	case T("yes"), T("no"):
		on := val == T("yes")
		tx := right - 46
		ty2 := r.y + (r.h-26)/2
		kind := uint8(snapToggleOff)
		if on {
			kind = snapToggleOn
		}
		s.cachedRect(kind, tx, ty2, 46, 26, func() {
			if on {
				s.fillRound(tx, ty2, 46, 26, 13, colAcc)
				s.disc(float64(tx+46-13), float64(ty2+13), 10, colOnAcc)
			} else {
				s.fillRound(tx, ty2, 46, 26, 13, rgb(0x3a, 0x3f, 0x48))
				s.disc(float64(tx+13), float64(ty2+13), 10, colDim)
			}
		})
		right = tx - 12
	case "":
		cx, cy := float64(right-4), float64(r.y)+float64(r.h)/2
		s.cachedRect(snapChevron, right-10, int(cy)-9, 16, 19, func() {
			s.stroke(cx-2.5, cy-5, cx+2, cy, 1.8, colDim)
			s.stroke(cx+2, cy, cx-2.5, cy+5, 1.8, colDim)
		})
		right -= 18
	default:
		val = f.fit(val, (r.w-2*uiCardPad)/2, 0)
		vw := f.width(val, 0)
		s.draw(f, right-vw, ty, val, colTeal2, 0)
		right -= vw + 12
	}
	s.draw(f, r.x+uiCardPad, ty, f.fit(name, right-r.x-uiCardPad, 0), fg, 0)
}

// drawPage renders a page and returns the buttons' rectangles for touch (zero for buttons not on the screen).
func (s *screen) drawPage(p page, sel int, status string) []rect {
	if p.centered {
		return s.drawCentered(p, sel)
	}
	W := s.w - 2*uiPad
	s.fill(0, 0, s.w, s.h, colBg)
	rects := make([]rect, len(p.buttons))

	// the main buttons at the bottom: everything that is not a key, a settings row or the back key
	var bottom []int
	for i, b := range p.buttons {
		if i >= p.grid && b.group == "" && b.kind != btnBack && !strings.Contains(b.label, "\n") {
			bottom = append(bottom, i)
		}
	}
	rowH := func(i int) int {
		b := p.buttons[i]
		switch {
		case b.kind == btnPrimary && !b.half:
			return 72
		case b.kind == btnDanger:
			return 64
		case b.half: // a pair with the primary button is as high as a lone danger one
			for _, o := range p.buttons {
				if o.half && o.kind == btnPrimary {
					return 64
				}
			}
		}
		return 56
	}
	type brow struct{ idx []int }
	var rows []brow
	for k := 0; k < len(bottom); k++ {
		i := bottom[k]
		if p.buttons[i].half && k+1 < len(bottom) && p.buttons[bottom[k+1]].half {
			rows = append(rows, brow{[]int{i, bottom[k+1]}})
			k++
			continue
		}
		rows = append(rows, brow{[]int{i}})
	}
	bottomH := 0
	for _, r := range rows {
		bottomH += rowH(r.idx[0]) + 12
	}
	foot := s.font.face(12, wRegular)
	footH := 0
	if p.foot != "" {
		footH = foot.height + 18
	}
	headTop := uiPad
	headH := s.font.face(24, wBold).height
	for _, b := range p.buttons {
		if b.kind == btnBack {
			headH = 44
		}
	}
	if p.icon != iconNone {
		headH = 36
	}
	if p.home {
		headH = 40
	}
	flowTop := headTop + headH + 18
	subF := s.font.face(14, wRegular)
	subLines := wrapPx(subF, p.sub, W, 3)
	if p.sub != "" {
		flowTop += len(subLines)*subF.height + 18
	}
	limit := s.h - uiPad - footH - bottomH
	// too many main buttons (a long list): they become part of the scrolling content
	listMode := len(rows) > 0 && limit-flowTop < 160
	if listMode {
		bottomH, limit = 0, s.h-uiPad-footH
	}

	// content: blocks, then settings rows (by group), then list buttons; laid out on a virtual column
	type item struct {
		y, h int
		draw func(off int)
		btn  int // the button drawn, -1 = none
	}
	var items []item
	y := 0
	add := func(h int, btn int, draw func(yy int)) {
		items = append(items, item{y: y, h: h, btn: btn, draw: draw})
		y += h
	}
	if p.total > 0 {
		p.lines = append(p.lines, line{bar: []int64{int64(p.done), int64(p.total)}})
	}
	for i := 0; i < len(p.lines); {
		j := i + 1
		for j < len(p.lines) && p.lines[j].box == p.lines[i].box && p.lines[i].box != boxNone {
			j++
		}
		run := p.lines[i:j]
		if i > 0 {
			gap := uiGap
			if run[0].box == boxNone && p.lines[i-1].box == boxNone {
				gap = blockGap(p.lines[i-1], run[0])
				if p.lines[i-1].size != sizeLabel {
					gap = max(gap, 10)
				}
			}
			y += gap
		}
		h := s.runH(run, W)
		add(h, -1, func(yy int) { s.drawRun(run, uiPad, yy, W) })
		i = j
	}
	// settings rows, one card per group
	fl := s.font.face(13, wRegular)
	for i := 0; i < len(p.buttons); {
		b := p.buttons[i]
		if i < p.grid || b.group == "" || b.kind == btnBack {
			i++
			continue
		}
		j := i + 1
		for j < len(p.buttons) && p.buttons[j].group == b.group {
			j++
		}
		if len(items) > 0 {
			y += uiGap
		}
		g := b.group
		add(fl.height+10, -1, func(yy int) { s.draw(fl, uiPad, yy, strings.ToUpper(g), colDim, 1) })
		top := y
		n := j - i
		rh := 56
		// the card behind the rows: its real height (for the off-screen check), the rows advance y
		items = append(items, item{y: y, h: n * rh, btn: -1, draw: func(yy int) { s.fillRound(uiPad, yy, W, n*rh, 16, colCard) }})
		for k := i; k < j; k++ {
			k := k
			add(rh, k, func(yy int) {
				if k > i {
					s.fill(uiPad, yy, W, 1, colKey)
				}
				s.settingRow(rect{uiPad, yy, W, rh}, p.buttons[k], k == sel)
			})
		}
		_ = top
		i = j
	}
	// list items: one card, a row each
	var items2 []int
	for i, b := range p.buttons {
		if i >= p.grid && b.group == "" && strings.Contains(b.label, "\n") {
			items2 = append(items2, i)
		}
	}
	if len(items2) > 0 {
		if len(items) > 0 {
			y += uiGap
		}
		n := len(items2)
		add(0, -1, func(yy int) { s.fillRound(uiPad, yy, W, n*74, 16, colCard) })
		for k, i := range items2 {
			k, i := k, i
			add(74, i, func(yy int) {
				if k > 0 {
					s.fill(uiPad, yy, W, 1, colKey)
				}
				s.listRow(rect{uiPad, yy, W, 74}, p.buttons[i], i == sel)
			})
		}
	}
	if listMode {
		for _, r := range rows {
			for _, i := range r.idx {
				i := i
				if len(items) > 0 {
					y += 10
				}
				add(rowH(i), i, func(yy int) { s.drawButton(rect{uiPad, yy, W, rowH(i)}, p.buttons[i], i == sel) })
			}
		}
	}
	// the key grid: the field, then the keys
	if p.grid > 0 {
		if len(items) > 0 {
			y += uiGap
		}
		if p.cols == 3 { // a code: one box per digit, 4 + 4
			digits := strings.ReplaceAll(p.field, "-", "")
			add(60, -1, func(yy int) {
				const n = 8
				bw := (W - 20 - 7*8) / n
				f := s.font.face(28, wBold)
				for i := 0; i < n; i++ {
					bx := uiPad + i*(bw+8)
					if i >= 4 {
						bx += 20
					}
					if i == len(digits) {
						s.box(bx, yy, bw, 60, 10, colCard, colAcc, 2)
					} else {
						s.box(bx, yy, bw, 60, 10, colCard, colBorder, 1)
					}
					if i < len(digits) {
						d := digits[i : i+1]
						s.draw(f, bx+(bw-f.width(d, 0))/2, yy+(60-f.height)/2, d, colText, 0)
					}
				}
				dash := s.font.face(20, wRegular)
				s.draw(dash, uiPad+4*(bw+8)+(20-8-dash.width("-", 0))/2, yy+(60-dash.height)/2, "-", colDim, 0)
			})
		} else {
			fieldF := s.font.face(17, wRegular)
			add(56, -1, func(yy int) {
				s.box(uiPad, yy, W, 56, 14, colCard, colAcc, 2)
				t := p.field + "_"
				for len(t) > 1 && fieldF.width(t, 0) > W-36 {
					t = string([]rune(t)[1:])
				}
				s.draw(fieldF, uiPad+18, yy+(56-fieldF.height)/2, t, colText, 0)
			})
		}
		cols, kh := gridCols, 52
		if p.cols > 0 {
			cols, kh = p.cols, 64
		}
		kw := (W - (cols-1)*10) / cols
		nrows := (p.grid + cols - 1) / cols
		gridH := nrows*kh + (nrows-1)*8
		// keys at the bottom of the free space (like the design), or right below the field
		if free := limit - flowTop - (y + 18 + gridH); free > 0 && p.cols == 3 {
			y += free
		}
		y += 18
		for i := 0; i < p.grid; i++ {
			i := i
			ky := y + i/cols*(kh+8)
			kx := uiPad + i%cols*(kw+10)
			items = append(items, item{y: ky, h: kh, btn: i, draw: func(yy int) {
				b := p.buttons[i]
				f := s.font.face(20, wRegular)
				if s.font.face(20, wRegular).width(b.label, 0) > kw-12 {
					f = s.font.face(15, wRegular)
				}
				bg, fg, ring := colKey, colText, colAcc
				if b.kind == btnPrimary {
					bg, fg, ring = colAcc, colOnAcc, colText
				}
				if i == sel {
					s.box(kx, yy, kw, kh, 10, bg, ring, 2)
				} else {
					s.fillRound(kx, yy, kw, kh, 10, bg)
				}
				s.draw(f, kx+(kw-f.width(b.label, 0))/2, yy+(kh-f.height)/2, b.label, fg, 0)
			}})
		}
		y += gridH
	}

	// scroll: keep the selected content item on the screen
	view := limit - flowTop
	off := 0
	if y > view && s.swiped { // scrolled by a swipe
		off = min(s.scroll, y-view)
		s.scroll = off
	} else if y > view {
		for _, it := range items {
			if it.btn == sel && it.btn >= 0 {
				if it.y+it.h > view-8 {
					off = it.y + it.h - (view - 8)
				}
			}
		}
		off = min(off, y-view)
	}
	s.off = off
	blit := y > view && y <= 8*s.h && !noBlit
	if blit {
		// scrolling content: drawn once into a surface as tall as the content, then a swipe frame only copies the
		// visible window (the content is unchanged while the user scrolls)
		key := s.pageKey // set by the caller for this page, saves building it twice per frame
		if key == "" {
			key = frameKey(p, sel, "", nil)
		}
		s.pageKey = ""
		if key != s.tallKey || len(s.tall) != s.w*y {
			s.tallKey = key
			s.tall = make([]uint16, s.w*y)
			pix, h := s.pix, s.h
			s.pix, s.h = s.tall, y
			s.fill(0, 0, s.w, y, colBg)
			for _, it := range items {
				it.draw(it.y)
			}
			s.pix, s.h = pix, h
		}
		copy(s.pix[flowTop*s.w:limit*s.w], s.tall[off*s.w:(off+view)*s.w])
	} else {
		s.tallKey = ""
	}
	for _, it := range items {
		yy := flowTop + it.y - off
		if yy+it.h < flowTop-40 || yy > limit+40 {
			continue
		}
		if !blit {
			it.draw(yy)
		}
		if it.btn >= 0 && yy >= flowTop-2 && yy+it.h <= limit+2 {
			kx := it.btn
			rects[kx] = rect{uiPad, yy, W, it.h}
		}
	}
	// key rectangles are narrower than the row
	if p.grid > 0 {
		cols, kh := gridCols, 52
		if p.cols > 0 {
			cols, kh = p.cols, 64
		}
		kw := (W - (cols-1)*10) / cols
		for i := 0; i < p.grid; i++ {
			if rects[i].w > 0 {
				rects[i] = rect{uiPad + i%cols*(kw+10), rects[i].y, kw, kh}
			}
		}
	}
	if y > view { // a scroll hint: the content goes on
		s.fill(0, 0, s.w, flowTop, colBg)
		s.fill(0, limit, s.w, s.h-limit, colBg)
		if off < y-view {
			s.fillRound(s.w/2-20, limit-8, 40, 4, 2, colBorder)
		}
		// a scroll bar at the right edge: where the view is in the content
		bh := max(24, view*view/y)
		s.fillRound(s.w-6, flowTop+(view-bh)*off/(y-view), 3, bh, 1, colBorder)
	}

	// header
	switch {
	case p.home:
		s.cached(0, uiPad, headTop, 40, func() { s.logo(uiPad, headTop, 40) })
		f := s.font.face(24, wBold)
		s.draw(f, uiPad+52, headTop+(40-f.height)/2, "SyncMusic", colText, 0)
	default:
		x := uiPad
		for i, b := range p.buttons {
			if b.kind == btnBack {
				r := rect{uiPad, headTop, 44, 44}
				rects[i] = r
				s.drawButton(r, b, i == sel)
				x += 56
				break
			}
		}
		if p.icon != iconNone {
			s.cached(p.icon, x, headTop, 36, func() { s.icon(p.icon, x, headTop, 36) })
			x += 48
		}
		f := s.font.face(24, wBold)
		sf := s.font.face(14, wRegular)
		maxW := s.w - uiPad - x
		if status != "" && p.showStatus {
			maxW -= sf.width(status, 0) + 16
		}
		s.draw(f, x, headTop+(headH-f.height)/2, f.fit(p.title, maxW, 0), colText, 0)
	}
	if status != "" && (p.home || p.showStatus) {
		sf := s.font.face(14, wRegular)
		s.draw(sf, s.w-uiPad-sf.width(status, 0), headTop+(headH-sf.height)/2, status, colSoft, 0)
	}
	for i, ln := range subLines {
		s.draw(subF, uiPad, headTop+headH+18+i*subF.height, ln, colSoft, 0)
	}

	// the main buttons and the hint
	by := s.h - uiPad - footH
	if p.foot != "" {
		ln := foot.fit(p.foot, W, 0)
		s.draw(foot, (s.w-foot.width(ln, 0))/2, by+18, ln, colDim, 0)
	}
	if !listMode {
		for k := len(rows) - 1; k >= 0; k-- {
			r := rows[k]
			h := rowH(r.idx[0])
			by -= h
			if len(r.idx) == 2 {
				hw := (W - 12) / 2
				for n, i := range r.idx {
					rr := rect{uiPad + n*(hw+12), by, hw, h}
					rects[i] = rr
					s.drawButton(rr, p.buttons[i], i == sel)
				}
			} else {
				i := r.idx[0]
				rr := rect{uiPad, by, W, h}
				rects[i] = rr
				s.drawButton(rr, p.buttons[i], i == sel)
			}
			by -= 12
		}
	}
	s.flush()
	return rects
}

// drawCentered: a page in the middle of the screen (Wi-Fi off, a short message): icon, title, text, buttons, hint.
func (s *screen) drawCentered(p page, sel int) []rect {
	s.fill(0, 0, s.w, s.h, colBg)
	rects := make([]rect, len(p.buttons))
	W := s.w - 2*32
	ft, fb, fh := s.font.face(28, wBold), s.font.face(16, wRegular), s.font.face(13, wRegular)
	var text []string
	for _, l := range p.lines {
		text = append(text, wrapPx(fb, l.text, W, 6)...)
	}
	h := 0
	if p.icon != iconNone {
		h += 64 + 22
	}
	h += ft.height + 16 + len(text)*(fb.height+2) + 20
	for i := range p.buttons {
		if p.buttons[i].kind == btnPrimary {
			h += 68 + 20
		} else {
			h += 56 + 20
		}
	}
	if p.foot != "" {
		h += fh.height
	}
	y := max(uiPad, (s.h-h)/2)
	if p.icon != iconNone {
		s.cached(p.icon, (s.w-64)/2, y, 64, func() { s.icon(p.icon, (s.w-64)/2, y, 64) })
		y += 64 + 22
	}
	t := ft.fit(p.title, W, 0)
	s.draw(ft, (s.w-ft.width(t, 0))/2, y, t, colText, 0)
	y += ft.height + 16
	for _, ln := range text {
		s.draw(fb, (s.w-fb.width(ln, 0))/2, y, ln, colSoft, 0)
		y += fb.height + 2
	}
	y += 18
	for i, b := range p.buttons {
		bh := 56
		if b.kind == btnPrimary {
			bh = 68
		}
		r := rect{32, y, W, bh}
		rects[i] = r
		s.drawButton(r, b, i == sel)
		y += bh + 20
	}
	if p.foot != "" {
		ln := fh.fit(p.foot, W, 0)
		s.draw(fh, (s.w-fh.width(ln, 0))/2, y, ln, colDim, 0)
	}
	s.flush()
	return rects
}
