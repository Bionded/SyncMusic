// Primary way of getting the player's library up to date after a sync: the server builds the tagcache for exactly
// what is on the card and the app replaces the whole library database with it (generation 1), so the player boots straight into it
// (no rescan). Format notes are in syncmusic/devices/HibyR1/compas_feedback.py.
package main

import (
	"bytes"
	"encoding/binary"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
)

// database_N.tcd / database_idx.tcd plus the derived indexes sdtool writes next to the master
// (database_idx.tcd.gN.ipath.v7, …): (base, generation suffix, index suffix).
var tcdName = regexp.MustCompile(`^(database_(?:idx|\d+)\.tcd)(\.g\d+)?(\.i[a-z0-9_]+(?:\.v\d+)?)?$`)

// writeSynced writes path via path.tmp, fsync, rename.
func writeSynced(path string, b []byte) error {
	tmp := path + ".tmp"
	f, err := os.Create(tmp)
	if err != nil {
		return err
	}
	if _, err = f.Write(b); err == nil {
		err = f.Sync()
	}
	if cerr := f.Close(); err == nil {
		err = cerr
	}
	if err != nil {
		os.Remove(tmp)
		return err
	}
	return os.Rename(tmp, path)
}

// writeFresh creates path (it must not exist), writes b and fsyncs it: no temp name and no rename, so a swap in
// .compas/ costs one directory entry per file. The player's exFAT driver corrupted .compas/ on the real R1 when
// many create+rename-over operations ran in it while the player had the directory in use.
func writeFresh(path string, b []byte) error {
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
	if err != nil {
		return err
	}
	if _, err = f.Write(b); err == nil {
		err = f.Sync()
	}
	if cerr := f.Close(); err == nil {
		err = cerr
	}
	if err != nil {
		os.Remove(path)
	}
	return err
}

func syncDir(dir string) {
	if d, err := os.Open(dir); err == nil {
		d.Sync()
		d.Close()
	}
}

// oldTagcache matches every file of the player's library database in .compas/ (any generation): the tables, the
// master index and its derived indexes, the refs files and their temp leftovers. Everything else there (device.json,
// albumart/, catalog.incarnation, playlists.list, queue.bin, …) belongs to the player and stays.
var oldTagcache = regexp.MustCompile(`^(database_(?:idx|\d+)\.tcd(\..*)?|tagcache\.refs(\..*)?)$`)

// installTagcache replaces the card's whole library database with the generation-1 set in src (as compas-sdtool
// builds it on an empty .compas/): the set is checked, every old database file is removed, the new files are
// written, and tagcache.gen = 1 goes last. No generation is renamed (the generation is stored inside several index
// files). An interrupted swap leaves no valid database, so the player starts fresh and rescans — never a corrupt
// state. Returns the installed generation (always 1).
func installTagcache(root, src string) (int, error) {
	ents, err := os.ReadDir(src)
	if err != nil {
		return 0, err
	}
	files := map[string][]byte{}
	for _, e := range ents {
		m := tcdName.FindStringSubmatch(e.Name())
		if m == nil || !e.Type().IsRegular() {
			continue
		}
		if m[2] != ".g1" {
			return 0, fmt.Errorf("the downloaded tagcache is not generation 1 (%s)", e.Name())
		}
		if m[3] != "" && m[1] != "database_idx.tcd" {
			continue
		}
		if files[e.Name()], err = os.ReadFile(filepath.Join(src, e.Name())); err != nil {
			return 0, err
		}
	}
	master, names := files["database_idx.tcd.g1"], files["database_4.tcd.g1"]
	if len(master) < 24 || len(names) < 12 {
		return 0, errors.New("the downloaded tagcache is incomplete")
	}
	le := binary.LittleEndian
	if m := le.Uint32(master); (m != 0x54434810 && m != 0x54434811) || le.Uint32(names) != 0x54434810 {
		return 0, errors.New("the downloaded tagcache has an unknown format")
	}
	if le.Uint32(master[20:]) != 0 {
		return 0, errors.New("the downloaded tagcache is marked dirty")
	}
	db := filepath.Join(root, ".compas")
	if err := os.MkdirAll(db, 0o755); err != nil {
		return 0, err
	}
	// pointer first: while it is gone the player finds no database and rescans
	if err := os.Remove(filepath.Join(db, "tagcache.gen")); err != nil && !os.IsNotExist(err) {
		return 0, err
	}
	old, err := os.ReadDir(db)
	if err != nil {
		return 0, err
	}
	for _, e := range old {
		if e.Type().IsRegular() && oldTagcache.MatchString(e.Name()) {
			if err := os.Remove(filepath.Join(db, e.Name())); err != nil {
				return 0, err
			}
		}
	}
	syncDir(db)
	// on any error below remove what was written: the card is left with no database (the player rescans), never
	// with a half one
	written := []string{}
	fail := func(err error) (int, error) {
		for _, p := range written {
			os.Remove(p)
		}
		syncDir(db)
		return 0, err
	}
	order := make([]string, 0, len(files))
	for name := range files {
		order = append(order, name)
	}
	sort.Strings(order)
	for _, name := range order {
		p := filepath.Join(db, name)
		os.Remove(p) // a leftover the player wrote meanwhile; the new file is created, never renamed over
		if err := writeFresh(p, files[name]); err != nil {
			return fail(err)
		}
		written = append(written, p)
	}
	syncDir(db)
	// read every file back before the pointer goes in: a set that is not complete on the card is never activated
	for _, name := range order {
		got, err := os.ReadFile(filepath.Join(db, name))
		if err != nil || !bytes.Equal(got, files[name]) {
			return fail(fmt.Errorf("the player's library file %s did not stay intact on the card", name))
		}
	}
	gen := filepath.Join(db, "tagcache.gen")
	if err := writeFresh(gen, []byte("1")); err != nil {
		return fail(err)
	}
	written = append(written, gen)
	syncDir(db)
	return 1, nil
}

// setMtime gives a written file the modification time the server built the tagcache with, so the player's
// lookup (path + mtime + size) hits without reading the file again. 0 = the server did not send one.
func setMtime(path string, unix int64) error {
	if unix <= 0 {
		return nil
	}
	t := time.Unix(unix/2*2, 0) // FAT keeps 2 s steps; the server builds with even seconds
	return os.Chtimes(path, t, t)
}

var audioExt = map[string]bool{".mp3": true, ".flac": true, ".m4a": true, ".m4b": true, ".aac": true, ".ogg": true,
	".opus": true, ".wav": true, ".aif": true, ".aiff": true, ".ape": true, ".wv": true, ".dsf": true, ".dff": true,
	".wma": true, ".alac": true, ".mka": true}

// foreignAudio counts the audio files on the card that this app did not put there (hidden folders skipped).
// The swapped-in database does not list them; the player adds them back on a rescan.
func (c *Client) foreignAudio() int {
	n := 0
	filepath.WalkDir(c.root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if d.IsDir() {
			if p != c.root && strings.HasPrefix(d.Name(), ".") {
				return filepath.SkipDir
			}
			return nil
		}
		if !audioExt[strings.ToLower(filepath.Ext(d.Name()))] {
			return nil
		}
		rel, err := filepath.Rel(c.root, p)
		if err != nil {
			return nil
		}
		if _, ours := c.st.Files[filepath.ToSlash(rel)]; !ours {
			n++
		}
		return nil
	})
	return n
}

// foreignNote is shown on the result screen when the swapped database left out files.
func foreignNote(n int) string {
	if n == 0 {
		return ""
	}
	return fmt.Sprintf("This card also has music SyncMusic does not manage (%s): rescan the library on the "+
		"player (Settings → Library → Rescan) to add it back — its hearts and play counts on the player are lost.", Count(n, "file", "files"))
}

// genSet: files the server built for the card, installed as one set; the commit file holds SetID once it is in.
type genSet struct {
	SetID  string `json:"set_id"`
	Commit string `json:"commit"`
	Files  []item `json:"files"`
}

// setInstalled: the commit file holds the set id (first line) and the names of the set's files in .compas/ (one
// per line); the set counts as installed only while every one of them is still there. A commit file without the
// names (older app) or a missing file means the set is fetched and installed again.
func (c *Client) setInstalled(commit string, g genSet) bool {
	b, err := os.ReadFile(commit)
	if err != nil {
		return false
	}
	lines := strings.Split(strings.TrimSpace(string(b)), "\n")
	if strings.TrimSpace(lines[0]) != g.SetID || len(lines) < 2 {
		return false
	}
	for _, n := range lines[1:] {
		if n = strings.TrimSpace(n); n == "" {
			continue
		}
		if _, err := os.Stat(filepath.Join(c.root, ".compas", n)); err != nil {
			fmt.Printf("generated set %s: %s is missing on the card, installing the set again\n", g.SetID, n)
			return false
		}
	}
	return true
}

// listDB prints the card's library database files (stdout goes to .apps/.last-session.log) for diagnosis.
func listDB(root, when string) {
	ents, _ := os.ReadDir(filepath.Join(root, ".compas"))
	names := []string{}
	for _, e := range ents {
		if oldTagcache.MatchString(e.Name()) || e.Name() == "tagcache.gen" {
			names = append(names, e.Name())
		}
	}
	fmt.Printf("library database %s: %d files: %s\n", when, len(names), strings.Join(names, " "))
}

// installGenerated fetches a generated set (unless it is installed and complete, see setInstalled), replaces the
// card's library database with the tagcache files in it and writes the commit file last. Every file of the set
// must arrive with its hash (items are keyed by path; same-content files are separate downloads), otherwise the
// old database stays. Returns the generation (0 = the set was already in).
func (c *Client) installGenerated(g genSet) (int, error) {
	commit, err := c.target(g.Commit)
	if err != nil {
		return 0, err
	}
	if c.setInstalled(commit, g) {
		return 0, nil
	}
	dir := ""
	names := []string{}
	for _, f := range g.Files {
		ok, err := c.download(f)
		if err != nil {
			return 0, err
		}
		if !ok {
			return 0, fmt.Errorf("generated file %s did not arrive intact", f.Path)
		}
		t, _ := c.target(f.Path)
		if d := filepath.Dir(t); dir == "" {
			dir = d
		} else if d != dir {
			return 0, errors.New("the generated files are not in one folder")
		}
		if m := tcdName.FindStringSubmatch(filepath.Base(t)); m != nil && (m[3] == "" || m[1] == "database_idx.tcd") {
			names = append(names, filepath.Base(t))
		}
	}
	if dir == "" {
		return 0, errors.New("the generated set is empty")
	}
	gen, err := installTagcache(c.root, dir)
	if err != nil {
		return 0, err
	}
	listDB(c.root, "after the install")
	for _, f := range g.Files { // the staged copies are not needed any more
		if t, err := c.target(f.Path); err == nil {
			os.Remove(t)
		}
	}
	if err := os.MkdirAll(filepath.Dir(commit), 0o755); err != nil {
		return 0, err
	}
	sort.Strings(names)
	return gen, writeSynced(commit, []byte(g.SetID+"\n"+strings.Join(names, "\n")+"\n"))
}
