// Listening data on the card → events: play counts and favourites from the player's tagcache
// (.compas/, database format 8), positions from the Audiobooks and Podcasts plugins' state files.
// Port of the read side of syncmusic/devices/HibyR1/compas_feedback.py; formats documented there.
package main

import (
	"bytes"
	"encoding/binary"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

const (
	playerRoot = "/data/mnt/sd_0/"
	tagCount   = 23
	entrySize  = (tagCount + 1) * 4
	tagFile    = 4
	tagPlays   = 15
	tagRating  = 16
	tagLast    = 18
)

// Listen is what the card says about one of our files.
type Listen struct {
	Plays    int     `json:"plays,omitempty"`
	Fav      bool    `json:"fav,omitempty"`
	Pos      float64 `json:"pos,omitempty"`
	Finished bool    `json:"finished,omitempty"`
	Last     int64   `json:"last,omitempty"` // last played (unix seconds, the player's clock)
}

func suffix(gen int) string {
	if gen > 0 {
		return ".g" + strconv.Itoa(gen)
	}
	return ""
}

func fnv1a(b []byte) uint32 {
	h := uint32(0x811c9dc5)
	for _, c := range b {
		h = (h ^ uint32(c)) * 0x01000193
	}
	return h
}

// readTagcache: card-relative path -> (play count, favourite) of the live entries; nil when there is none.
func readTagcache(root string) (map[string]Listen, error) {
	tc, err := loadTagcache(root)
	if tc == nil || err != nil {
		return nil, err
	}
	out := map[string]Listen{}
	for p, r := range tc.rows {
		out[p] = r.Listen
	}
	return out, nil
}

type tcRow struct {
	Listen
	slot int
}

// tagcache: the live master index file and our rows in it (card-relative path -> slot + stats).
type tagcache struct {
	master string
	rows   map[string]tcRow
}

func loadTagcache(root string) (*tagcache, error) {
	db := filepath.Join(root, ".compas")
	gen := 0
	if b, err := os.ReadFile(filepath.Join(db, "tagcache.gen")); err == nil {
		if g, err := strconv.Atoi(strings.TrimSpace(string(b))); err == nil && g > 0 {
			gen = g
		}
	}
	masterPath := filepath.Join(db, "database_idx.tcd"+suffix(gen))
	master, err := os.ReadFile(masterPath)
	if os.IsNotExist(err) {
		return nil, nil
	} else if err != nil {
		return nil, err
	}
	ngen := gen // the refs file may point the file name tag at an older generation
	if raw, err := os.ReadFile(filepath.Join(db, "tagcache.refs.g"+strconv.Itoa(gen))); err == nil && gen > 0 && len(raw) == 4*14 {
		u := func(i int) uint32 { return binary.LittleEndian.Uint32(raw[i*4:]) }
		src := int(int32(u(3 + 9)))
		if u(0) == 0x54435231 && u(1) == 1 && int(int32(u(2))) == gen && u(13) == fnv1a(raw[:52]) && src >= 0 && src <= gen {
			ngen = src
		}
	}
	names, err := os.ReadFile(filepath.Join(db, "database_4.tcd"+suffix(ngen)))
	if err != nil {
		return nil, errors.New("file name tag file missing")
	}
	i32 := func(b []byte, off int) int { return int(int32(binary.LittleEndian.Uint32(b[off:]))) }
	if len(master) < 24 {
		return nil, errors.New("truncated master index")
	}
	magic, count, dirty := uint32(i32(master, 0)), i32(master, 8), i32(master, 20)
	if magic != 0x54434810 && magic != 0x54434811 {
		return nil, errors.New("not a Compás tagcache of a known format")
	}
	if dirty != 0 {
		return nil, errors.New("the tagcache is being written")
	}
	if count < 0 || 24+count*entrySize > len(master) || len(names) < 12 || uint32(i32(names, 0)) != 0x54434810 {
		return nil, errors.New("tagcache damaged")
	}
	out := map[string]tcRow{}
	for slot := 0; slot < count; slot++ {
		e := master[24+slot*entrySize:]
		if i32(e, tagCount*4)&1 != 0 {
			continue
		}
		seek := i32(e, tagFile*4)
		if seek < 12 || seek+8 > len(names) {
			continue
		}
		n, idx := i32(names, seek), i32(names, seek+4)
		if idx != slot || n <= 0 || seek+8+n > len(names) {
			continue
		}
		raw := names[seek+8 : seek+8+n]
		if z := bytes.IndexByte(raw, 0); z >= 0 {
			raw = raw[:z]
		}
		if p, ok := strings.CutPrefix(string(raw), playerRoot); ok {
			out[p] = tcRow{Listen{Plays: max(i32(e, tagPlays*4), 0), Fav: i32(e, tagRating*4) == 1,
				Last: int64(max(i32(e, tagLast*4), 0))}, slot}
		}
	}
	return &tagcache{masterPath, out}, nil
}

// Wanted is the library's listening state of one item (the plan's `state` section).
type Wanted struct {
	Key       string   `json:"key"`
	Favourite bool     `json:"favourite"`
	PlayCount int      `json:"play_count"`
	PositionS *float64 `json:"position_s,omitempty"` // podcasts and audiobooks
	Finished  bool     `json:"finished,omitempty"`
}

type patch struct {
	card string // our card path (state.json key)
	slot int
	to   Listen
}

// heartsDown: the rows of our files whose heart the library changed or whose play count is below the library's.
// Keys in `skip` (hearts the player just sent up, which the library may not have taken yet) keep their heart.
// Brand-new files are not indexed yet: their stats land on a later run. apply() makes the library's hearts the
// baseline once they are on the player.
func (c *Client) heartsDown(want []Wanted, skip map[string]bool) (*downPlan, error) {
	return c.down(want, skip, nil)
}

// down: hearts and play counts (heartsDown) plus the positions to write (positionsDown; keys in skipPos were
// just sent up by the player).
func (c *Client) down(want []Wanted, skip, skipPos map[string]bool) (*downPlan, error) {
	pos := c.positionsDown(want, skipPos)
	tc, err := loadTagcache(c.root)
	if tc == nil || err != nil {
		return &downPlan{positions: pos}, err
	}
	d := &downPlan{tc: tc, positions: pos}
	byKey := map[string]Wanted{}
	for _, w := range want {
		byKey[w.Key] = w
	}
	for card, key := range c.st.Keys {
		w, ok := byKey[key]
		r, indexed := tc.rows[cardPath(card)]
		if !ok || !indexed || skip[key] {
			continue // a skipped heart is looked at again next time
		}
		d.decided = append(d.decided, w)
		to := r.Listen
		// a heart goes down when the library's changed since the last look; on the first look only an added
		// one (a library without hearts must not clear the player's)
		before, known := c.st.Library[key]
		if (known && before != w.Favourite) || (!known && w.Favourite) {
			to.Fav = w.Favourite
		}
		to.Plays = max(to.Plays, w.PlayCount)
		if to.Fav != r.Fav || to.Plays != r.Plays {
			d.patches = append(d.patches, patch{card, r.slot, to})
		}
	}
	sort.Slice(d.patches, func(i, j int) bool { return d.patches[i].slot < d.patches[j].slot })
	return d, nil
}

// downPlan: what heartsDown found — the rows to patch and the library states it decided on.
type downPlan struct {
	tc        *tagcache
	patches   []patch
	decided   []Wanted
	positions map[string]Listen // card path -> library position (Audiobooks/, Podcasts/)
}

// apply writes the patches and makes the library's hearts the baseline for the next look.
func (c *Client) apply(d *downPlan) error {
	if n, err := c.writeEpisodes(); err != nil { // first: a position goes only to an episode the plugin lists
		return err
	} else if n > 0 {
		fmt.Fprintf(os.Stderr, "%d podcast episode(s) not listed: the plugin's list is full\n", n)
	}
	if dropped, err := c.writePositions(d.positions, nowUnix()); err != nil {
		return err
	} else if len(dropped) > 0 {
		fmt.Fprintf(os.Stderr, "%d audiobook position(s) left out: the plugin's state file is full\n", len(dropped))
	}
	if d.tc == nil {
		return c.save()
	}
	if err := c.applyPatches(d.tc, d.patches); err != nil {
		return err
	}
	if c.st.Library == nil {
		c.st.Library = map[string]bool{}
	}
	for _, w := range d.decided {
		c.st.Library[w.Key] = w.Favourite
	}
	return c.save()
}

// applyPatches writes rating (1 = heart) and play count in place into the live master index, as the player's
// own numeric journal does, and moves the baseline so the next look does not send them back up. Only right
// before a restart: the running player keeps its own copy and would write over a row it updates later.
func (c *Client) applyPatches(tc *tagcache, ps []patch) error {
	if len(ps) == 0 {
		return nil
	}
	f, err := os.OpenFile(tc.master, os.O_RDWR, 0)
	if err != nil {
		return err
	}
	defer f.Close()
	if c.st.Seen == nil {
		c.st.Seen = map[string]Listen{}
	}
	head := make([]byte, 24)
	if _, err := f.ReadAt(head, 0); err != nil {
		return err
	}
	if binary.LittleEndian.Uint32(head[20:]) != 0 {
		return errors.New("the tagcache is being written")
	}
	put := func(off int64, v int32) error {
		b := binary.LittleEndian.AppendUint32(nil, uint32(v))
		_, err := f.WriteAt(b, off)
		return err
	}
	for _, p := range ps {
		base := int64(24 + p.slot*entrySize)
		rating := int32(0)
		if p.to.Fav {
			rating = 1
		}
		if err := put(base+tagRating*4, rating); err != nil {
			return err
		}
		if err := put(base+tagPlays*4, int32(p.to.Plays)); err != nil {
			return err
		}
		seen := c.st.Seen[p.card]
		seen.Fav, seen.Plays = p.to.Fav, p.to.Plays
		c.st.Seen[p.card] = seen
	}
	if err := f.Sync(); err != nil {
		return err
	}
	return c.save()
}

func tsv(path string) [][]string {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var out [][]string
	for _, line := range strings.Split(string(b), "\n") {
		if strings.HasPrefix(line, "P\t") {
			out = append(out, strings.Split(strings.TrimRight(line, "\r"), "\t"))
		}
	}
	return out
}

func unq(s string) string {
	if v, err := url.PathUnescape(s); err == nil {
		return v
	}
	return s
}

// positions: card-relative path -> position of what the plugins saw played (a 0 s, unplayed record is no data).
func positions(root string) map[string]Listen {
	out := map[string]Listen{}
	num := func(s string) float64 { f, _ := strconv.ParseFloat(s, 64); return f }
	for _, p := range tsv(filepath.Join(root, "Podcasts", ".progress.tsv")) {
		if len(p) < 8 {
			continue
		}
		pos, fin := num(p[2]), p[4] == "1"
		if rel, ok := strings.CutPrefix(unq(p[6]), playerRoot); ok && (pos > 0 || fin || num(p[7]) > 0) {
			out[rel] = Listen{Pos: pos, Finished: fin}
		}
	}
	for _, p := range tsv(filepath.Join(root, ".plugins", ".audiobooks_state_v3")) {
		if len(p) < 7 {
			continue
		}
		key, file := unq(p[1]), unq(p[2])
		pos, fin := num(p[3]), p[5] == "1"
		if key == "" || file == "" || !(pos > 0 || fin || num(p[6]) > 0) {
			continue
		}
		rel := "Audiobooks/" + key + "/" + file
		if key == file {
			rel = "Audiobooks/" + file
		}
		out[rel] = Listen{Pos: pos, Finished: fin}
	}
	return out
}

// collect turns what changed on the card since the last look into queued events (our files only).
// The first look at a file compares with nothing played, so plays made before are counted once.
func (c *Client) collect() (int, error) {
	tc, err := readTagcache(c.root)
	if err != nil {
		return 0, err // a damaged or busy tagcache: keep the baseline, try next time
	}
	pos := positions(c.root)
	if c.st.Seen == nil {
		c.st.Seen = map[string]Listen{}
	}
	n := 0
	for card, key := range c.st.Keys {
		path := cardPath(card)
		now, before := tc[path], c.st.Seen[card]
		if p, ok := pos[path]; ok {
			now.Pos, now.Finished = p.Pos, p.Finished
		} else {
			now.Pos, now.Finished = before.Pos, before.Finished
		}
		if tc == nil { // the player has not indexed the card yet
			now.Plays, now.Fav, now.Last = before.Plays, before.Fav, before.Last
		}
		for _, at := range playTimes(before.Last, now.Last, now.Plays-before.Plays, time.Now()) {
			c.queueAt("play", key, at, map[string]any{"completed": true})
			n++
		}
		if now.Fav != before.Fav {
			c.queue("favourite", key, map[string]any{"on": now.Fav})
			n++
		}
		if now.Pos != before.Pos || now.Finished != before.Finished {
			c.queue("position", key, map[string]any{"position_s": now.Pos, "finished": now.Finished})
			n++
		}
		c.st.Seen[card] = now
	}
	return n, c.save()
}

// playTimes: when the n plays counted since the last look happened. The tagcache only keeps the time of the
// latest one: that one gets it, the earlier ones are spread evenly between the previous look's last-played
// time and it (a minute apart when there is none), in order. No last-played time: all at `now`.
func playTimes(prev, last int64, n int, now time.Time) []time.Time {
	if n <= 0 {
		return nil
	}
	out := make([]time.Time, n)
	if last <= 0 || last > now.Unix()+86400 {
		for i := range out {
			out[i] = now.Add(time.Duration(i-n+1) * time.Second)
		}
		return out
	}
	step := int64(60)
	if prev > 0 && prev < last {
		step = max((last-prev)/int64(n), 1)
	}
	for i := range out {
		out[i] = time.Unix(last-int64(n-1-i)*step, 0).UTC()
	}
	return out
}
