package main

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

// rangeServer serves `data` and answers 416 to a Range that starts at or past its end (as Starlette does).
func rangeServer(t *testing.T, data []byte, ranges *[]string) *httptest.Server {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rg := r.Header.Get("Range")
		*ranges = append(*ranges, rg)
		if rg != "" {
			from, _ := strconv.Atoi(strings.TrimSuffix(strings.TrimPrefix(rg, "bytes="), "-"))
			if from >= len(data) {
				w.WriteHeader(http.StatusRequestedRangeNotSatisfiable)
				return
			}
			w.Header().Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", from, len(data)-1, len(data)))
			w.WriteHeader(http.StatusPartialContent)
			w.Write(data[from:])
			return
		}
		w.Write(data)
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestDownloadRecoversFromALeftoverPart(t *testing.T) {
	data := []byte(strings.Repeat("music-bytes ", 100))
	sum := sha256.Sum256(data)
	it := item{Path: "Music/A/one.mp3", Fid: hex.EncodeToString(sum[:]), Size: int64(len(data))}
	for name, part := range map[string][]byte{
		"complete":            data,
		"too big":             append(append([]byte{}, data...), "junk"...),
		"same size but wrong": []byte(strings.Repeat("x", len(data))),
		"short and stale":     data[:10],
	} {
		t.Run(name, func(t *testing.T) {
			it := it
			var ranges []string
			srv := rangeServer(t, data, &ranges)
			root := t.TempDir()
			if name == "short and stale" { // the server's file changed: its Range answer is out of range
				srv = rangeServer(t, data[:5], &ranges)
				sum5 := sha256.Sum256(data[:5])
				it = item{Path: it.Path, Fid: hex.EncodeToString(sum5[:]), Size: 5}
			}
			c := &Client{root: root, http: srv.Client(), st: State{Server: srv.URL}}
			dest, _ := c.target(it.Path)
			os.MkdirAll(filepath.Dir(dest), 0o755)
			os.WriteFile(dest+".part", part, 0o644)
			ok, err := c.download(it)
			if err != nil || !ok {
				t.Fatalf("download: ok=%v err=%v ranges=%q", ok, err, ranges)
			}
			got, _ := os.ReadFile(dest)
			if int64(len(got)) != it.Size {
				t.Fatalf("dest has %d bytes, want %d", len(got), it.Size)
			}
			if _, err := os.Stat(dest + ".part"); !os.IsNotExist(err) {
				t.Fatal(".part left behind")
			}
			if name == "complete" && len(ranges) != 0 {
				t.Fatalf("a complete .part needs no request, got %q", ranges)
			}
		})
	}
}

func TestDownloadProgressCountsAWholeAnswerOnce(t *testing.T) {
	data := []byte(strings.Repeat("music-bytes ", 100))
	sum := sha256.Sum256(data)
	it := item{Path: "Music/A/one.mp3", Fid: hex.EncodeToString(sum[:]), Size: int64(len(data))}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write(data) // ignores Range: answers 200 with the whole file
	}))
	t.Cleanup(srv.Close)
	c := &Client{root: t.TempDir(), http: srv.Client(), st: State{Server: srv.URL}}
	dest, _ := c.target(it.Path)
	os.MkdirAll(filepath.Dir(dest), 0o755)
	os.WriteFile(dest+".part", data[:300], 0o644)
	ok, err := c.download(it)
	if err != nil || !ok {
		t.Fatalf("download: ok=%v err=%v", ok, err)
	}
	if got := c.pr.snapshot().Bytes; got != it.Size {
		t.Fatalf("progress counted %d bytes, want %d", got, it.Size)
	}
}
