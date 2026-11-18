// SPDX-License-Identifier: GPL-3.0-or-later
// fontgen rasterises Inter (SIL OFL 1.1) at the sizes the app uses into one small atlas (4-bit alpha), so the
// app draws antialiased text without a font engine at run time.
//
//	go run . <Inter-Regular.ttf> <Inter-SemiBold.ttf> <Inter-Bold.ttf> ../inter.atlas.gz
//
// Format (little endian): "SMF1", face count u8; per face: size u8, weight u8 (0 regular, 1 semibold, 2 bold),
// ascent u8, line height u8, glyph count u16; per glyph: rune u32, advance u16 (1/64 px), left i8, top i8 (from
// the baseline up), w u8, h u8. Then all glyph bitmaps in the same order, 4 bits per pixel, each row starting on a
// whole byte.
package main

import (
	"bytes"
	"compress/gzip"
	"encoding/binary"
	"fmt"
	"image"
	"image/draw"
	"os"
	"slices"

	"golang.org/x/image/font"
	"golang.org/x/image/font/opentype"
	"golang.org/x/image/font/sfnt"
	"golang.org/x/image/math/fixed"
)

// faces: size and weight of every text style of the mockups
var faces = []struct{ size, weight int }{
	{12, 0}, {13, 0}, {14, 0}, {15, 0}, {16, 0}, {17, 0}, {20, 0}, {22, 0},
	{13, 1}, {17, 1}, {18, 1}, {20, 1},
	{17, 2}, {22, 2}, {24, 2}, {28, 2}, {32, 2},
}

func runes() []rune {
	var rs []rune
	add := func(a, b rune) {
		for r := a; r <= b; r++ {
			rs = append(rs, r)
		}
	}
	add(0x20, 0x7e)
	add(0xa0, 0xff)
	add(0x400, 0x45f)
	add(0x490, 0x491)
	rs = append(rs, []rune("–—‘’“”…•−→←↑↓✓✕●○■□▶⏻⌫⚡‹›∞≥≤")...)
	slices.Sort(rs)
	return slices.Compact(rs)
}

func main() {
	if len(os.Args) != 5 {
		fmt.Fprintln(os.Stderr, "usage: fontgen REGULAR SEMIBOLD BOLD OUT")
		os.Exit(2)
	}
	var fonts [3]*opentype.Font
	for i := range fonts {
		b, err := os.ReadFile(os.Args[1+i])
		must(err)
		fonts[i], err = opentype.Parse(b)
		must(err)
	}
	var head, bits bytes.Buffer
	head.WriteString("SMF1")
	head.WriteByte(byte(len(faces)))
	var buf sfnt.Buffer
	for _, fc := range faces {
		f := fonts[fc.weight]
		face, err := opentype.NewFace(f, &opentype.FaceOptions{Size: float64(fc.size), DPI: 72, Hinting: font.HintingNone})
		must(err)
		m := face.Metrics()
		var gl []rune
		for _, r := range runes() {
			if gi, _ := f.GlyphIndex(&buf, r); gi != 0 || r == ' ' {
				gl = append(gl, r)
			}
		}
		head.Write([]byte{byte(fc.size), byte(fc.weight), byte(m.Ascent.Ceil()), byte(m.Height.Ceil())})
		must(binary.Write(&head, binary.LittleEndian, uint16(len(gl))))
		for _, r := range gl {
			dr, mask, mp, adv, ok := face.Glyph(fixed.Point26_6{}, r)
			if !ok {
				panic(fmt.Sprintf("glyph %q", r))
			}
			w, h := dr.Dx(), dr.Dy()
			must(binary.Write(&head, binary.LittleEndian, uint32(r)))
			must(binary.Write(&head, binary.LittleEndian, uint16(adv)))
			head.Write([]byte{byte(int8(dr.Min.X)), byte(int8(-dr.Min.Y)), byte(w), byte(h)})
			a := image.NewAlpha(image.Rect(0, 0, w, h))
			draw.Draw(a, a.Bounds(), mask, mp, draw.Src)
			for y := 0; y < h; y++ {
				for x := 0; x < w; x += 2 {
					v := a.AlphaAt(x, y).A >> 4
					if x+1 < w {
						v |= a.AlphaAt(x+1, y).A >> 4 << 4
					}
					bits.WriteByte(v)
				}
			}
		}
	}
	out, err := os.Create(os.Args[4])
	must(err)
	z, _ := gzip.NewWriterLevel(out, gzip.BestCompression)
	_, _ = z.Write(head.Bytes())
	_, _ = z.Write(bits.Bytes())
	must(z.Close())
	must(out.Close())
	fmt.Printf("%d faces, header %d B, bitmaps %d B\n", len(faces), head.Len(), bits.Len())
}

func must(err error) {
	if err != nil {
		panic(err)
	}
}
