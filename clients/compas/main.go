// SyncMusic client for the Compás firmware (HiBy R1) as a native external app.
// Port of syncmusic/reference_client.py: pair, sync into the SD card, send listening events.
//
//	syncmusic-compas pair <server> <code>   pair once (token kept in $COMPAS_APP_DATA)
//	syncmusic-compas sync                   one sync
//	syncmusic-compas play|favourite|position <key> [...]   record an event (sent with the next sync)
//
// SD root: $SYNCMUSIC_SD_ROOT, else the parent of the .apps-data folder ($COMPAS_APP_DATA/../..).
package main

import (
	"bytes"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// version is set at build time (-ldflags -X main.version=...).
var version = "dev"

const api = "api/client/v1"

type State struct {
	UID         string               `json:"uid"`
	Server      string               `json:"server,omitempty"`
	Token       string               `json:"token,omitempty"`
	DeviceID    string               `json:"device_id,omitempty"`
	Seq         int                  `json:"seq"`
	Events      []map[string]any     `json:"events"`
	Files       map[string]int64     `json:"files"`                     // what this app put on the card: path -> size
	Keys        map[string]string    `json:"keys"`                      // path -> item key (events refer to keys)
	Seen        map[string]Listen    `json:"seen"`                      // path -> listening data on the card at the last look
	Library     map[string]bool      `json:"library_hearts,omitempty"`  // key -> the library's heart at the last look
	LibRating   map[string]int       `json:"library_ratings,omitempty"` // key -> the library's rating at the last look
	Albums      map[string]AlbumBase `json:"albums,omitempty"`          // player album key -> agreed album rating
	Lists       map[string][]string  `json:"lists,omitempty"`           // card playlist -> item keys at the last look
	Episodes    map[string]Meta      `json:"episodes,omitempty"`        // our podcast files -> what the plugin lists
	LastSync    string               `json:"last_sync,omitempty"`       // local time of the last finished sync
	LastSummary string               `json:"last_summary,omitempty"`    // what it did
	LastGrid    []string             `json:"last_grid,omitempty"`       // the same as short counts for the home page
	History     []HistoryEntry       `json:"history,omitempty"`         // the last syncs, newest first
}

type Client struct {
	data, root string
	st         State
	http       *http.Client
	progress   func(done, total int, what string)
	cancel     func() bool        // the user asked to stop (UI key); nil = never
	pr         progressBox        // what the running sync is doing (read by the UI)
	preview    func(Preview) bool // asks the user before a sync starts (nil = go ahead)
}

func uuid4() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	b[6] = b[6]&0x0f | 0x40
	b[8] = b[8]&0x3f | 0x80
	h := hex.EncodeToString(b)
	return h[0:8] + "-" + h[8:12] + "-" + h[12:16] + "-" + h[16:20] + "-" + h[20:]
}

func open() (*Client, error) {
	data := os.Getenv("COMPAS_APP_DATA")
	if data == "" {
		return nil, errors.New("COMPAS_APP_DATA is not set")
	}
	root := os.Getenv("SYNCMUSIC_SD_ROOT")
	if root == "" {
		root = filepath.Dir(filepath.Dir(data))
	}
	c := &Client{data: data, root: root, http: &http.Client{Timeout: 0},
		progress: func(int, int, string) {}}
	if err := os.MkdirAll(data, 0o755); err != nil {
		return nil, err
	}
	b, err := os.ReadFile(filepath.Join(data, "state.json"))
	if err == nil {
		err = json.Unmarshal(b, &c.st)
	} else if os.IsNotExist(err) {
		c.st = State{UID: uuid4()}
		err = nil
	}
	if c.st.Files == nil {
		c.st.Files = map[string]int64{}
	}
	if c.st.Keys == nil {
		c.st.Keys = map[string]string{}
	}
	if c.st.Events == nil {
		c.st.Events = []map[string]any{}
	}
	return c, err
}

func (c *Client) save() error {
	b, _ := json.MarshalIndent(c.st, "", " ")
	return writeAtomic(filepath.Join(c.data, "state.json"), b)
}

func writeAtomic(path string, b []byte) error {
	tmp := path + ".tmp"
	f, err := os.Create(tmp)
	if err != nil {
		return err
	}
	if _, err = f.Write(b); err == nil {
		err = f.Sync()
	}
	f.Close()
	if err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

type ProtoError struct {
	Status int
	Code   string
	Msg    string
}

func (e *ProtoError) Error() string { return fmt.Sprintf("%d %s: %s", e.Status, e.Code, e.Msg) }

func (c *Client) url(p string) string {
	return strings.TrimRight(c.st.Server, "/") + "/" + api + "/" + p
}

func (c *Client) call(method, path string, body any, out any) error {
	return c.callH(method, path, nil, body, out)
}

func (c *Client) callH(method, path string, headers map[string]string, body any, out any) error {
	var rd io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rd = bytes.NewReader(b)
	}
	req, _ := http.NewRequest(method, c.url(path), rd)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if c.st.Token != "" {
		req.Header.Set("Authorization", "Bearer "+c.st.Token)
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	r, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer r.Body.Close()
	raw, _ := io.ReadAll(r.Body)
	if r.StatusCode >= 400 {
		var e struct {
			Error struct{ Code, Message string } `json:"error"`
		}
		_ = json.Unmarshal(raw, &e)
		return &ProtoError{r.StatusCode, e.Error.Code, e.Error.Message}
	}
	if out != nil && len(raw) > 0 {
		return json.Unmarshal(raw, out)
	}
	return nil
}

func (c *Client) capabilities() map[string]any {
	var s syscall.Statfs_t
	var total, free uint64
	if syscall.Statfs(c.root, &s) == nil {
		total, free = s.Blocks*uint64(s.Bsize), s.Bavail*uint64(s.Bsize)
	}
	return map[string]any{
		// what the Audiobooks/Podcasts plugins play (USB Compás: PODCAST_FORMATS); the protocol has one list for
		// every kind, so music in wav/aiff/ape/wv/dsd is converted here but kept over USB
		"formats":     []string{"mp3", "aac", "alac", "flac", "ogg", "opus"},
		"media_kinds": []string{"music", "podcast", "audiobook"},
		"storage":     map[string]any{"total": total, "free": free},
		"features":    map[string]any{"playlists": true, "play_events": true},
		"layout":      map[string]any{"stores_paths": true},
	}
}

func (c *Client) Pair(server, code, name string) error {
	c.st.Server = normalizeServer(server)
	var out struct {
		Token    string `json:"token"`
		DeviceID string `json:"device_id"`
	}
	if err := c.call("POST", "pair", map[string]any{"code": code, "client": c.clientInfo(name), "capabilities": c.capabilities()}, &out); err != nil {
		return err
	}
	c.st.Token, c.st.DeviceID = out.Token, out.DeviceID
	return c.save()
}

// target resolves a server path under the SD root and refuses anything outside it.
func (c *Client) target(rel string) (string, error) {
	p := filepath.Join(c.root, filepath.FromSlash(cardPath(rel)))
	root := filepath.Clean(c.root)
	if p == root || !strings.HasPrefix(p, root+string(filepath.Separator)) {
		return "", fmt.Errorf("unsafe path from the server: %q", rel)
	}
	return p, nil
}

// cardPath: where a server path lives on the card. The player reads and edits Playlists/*.m3u (UTF-8,
// paths relative to the playlist work), so the server's .m3u8 playlists are stored as .m3u.
func cardPath(rel string) string {
	if strings.HasPrefix(rel, "Playlists/") && strings.HasSuffix(strings.ToLower(rel), ".m3u8") {
		return rel[:len(rel)-1]
	}
	return rel
}

type item struct {
	Path string `json:"path"`
	Fid  string `json:"fid"`
	Size int64  `json:"size"`
	Key  string `json:"key"`
	// Mtime: the modification time the server's tagcache build used for this file (0 = none sent).
	Mtime int64 `json:"mtime,omitempty"`
	Meta  *Meta `json:"meta,omitempty"`
}

type Totals struct {
	FetchCount      int   `json:"fetch_count"`
	FetchBytes      int64 `json:"fetch_bytes"`
	DeleteCount     int   `json:"delete_count"`
	SkippedForSpace int   `json:"skipped_for_space"`
}

// Preview is what a sync is about to do, shown before it starts. Confirm: the server waits for the user's
// confirmation (Reason) and the answer goes to the server.
type Preview struct {
	Totals
	Playlists int
	Confirm   bool
	Reason    string
}

// errDeclined: the user said no to the preview.
var errDeclined = errors.New("sync cancelled before it started")

type view struct {
	SessionID  string  `json:"session_id"`
	Status     string  `json:"status"`
	Message    string  `json:"message"`
	Code       string  `json:"code"`
	RetryAfter float64 `json:"retry_after_s"`
	MayConfirm bool    `json:"may_confirm"` // needs_review that the user may confirm here
	Review     Totals  `json:"review"`
	Plan       struct {
		Totals Totals             `json:"totals"`
		Delete []item             `json:"delete"`
		Fetch  []item             `json:"fetch"`
		State  []Wanted           `json:"state"`
		Next   map[string]*string `json:"next"`
		// Generated: server-built files installed as one set (here: the card's tagcache), nil = none.
		Generated *genSet `json:"generated"`
		AfterSync string  `json:"after_sync"` // none | refresh | restart (after a sync that changed something)
		More      int     `json:"more"`       // files the server holds back for the next window
	} `json:"plan"`
}

// inventory: the files this app put on the card that are still there (never the user's own files).
func (c *Client) inventory() []map[string]any {
	paths := make([]string, 0, len(c.st.Files))
	for p := range c.st.Files {
		paths = append(paths, p)
	}
	sort.Strings(paths)
	inv := []map[string]any{}
	for _, p := range paths {
		t, err := c.target(p)
		if err != nil {
			continue
		}
		if fi, err := os.Stat(t); err == nil && fi.Mode().IsRegular() {
			inv = append(inv, map[string]any{"path": p, "size": fi.Size()})
		} else {
			delete(c.st.Files, p)
		}
	}
	return inv
}

func (c *Client) start() (*view, error) {
	v := &view{}
	body := map[string]any{"capabilities": c.capabilities(), "inventory": map[string]any{"kind": "full", "items": c.inventory()}}
	if err := c.call("POST", "sync", body, v); err != nil {
		return nil, err
	}
	asked := false
	for i := 0; i < 600; i++ {
		switch v.Status {
		case "ready":
			if c.preview != nil {
				lists := 0
				for _, it := range v.Plan.Fetch {
					if strings.HasPrefix(cardPath(it.Path), "Playlists/") {
						lists++
					}
				}
				if !c.preview(Preview{Totals: v.Plan.Totals, Playlists: lists}) {
					_ = c.call("POST", "sync/"+v.SessionID+"/finish", map[string]any{"status": "cancelled"}, nil)
					return nil, errDeclined
				}
			}
			return v, nil
		case "failed", "finished":
			code := v.Status
			if v.Code != "" {
				code = v.Code
			}
			return nil, &ProtoError{409, code, v.Message}
		case "needs_review":
			if v.MayConfirm && c.preview != nil && !asked {
				asked = true
				if !c.preview(Preview{Totals: v.Review, Confirm: true, Reason: v.Message}) {
					return nil, errDeclined
				}
				sid := v.SessionID
				v = &view{}
				if err := c.call("POST", "sync/"+sid+"/confirm", nil, v); err != nil {
					return nil, err
				}
				continue
			}
			c.progress(0, 0, "Confirm the sync on the server: "+v.Message)
		default:
			c.progress(0, 0, "Waiting for the server: "+v.Status)
		}
		wait := 2.0
		if v.RetryAfter > 0 && v.RetryAfter < wait {
			wait = v.RetryAfter
		}
		time.Sleep(time.Duration(wait * float64(time.Second)))
		sid := v.SessionID
		v = &view{}
		if err := c.call("GET", "sync/"+sid, nil, v); err != nil {
			return nil, err
		}
	}
	return nil, errors.New("no plan from the server")
}

func section[T any](c *Client, v *view, name string, first []T) ([]T, error) {
	items := append([]T{}, first...)
	cur := v.Plan.Next[name]
	for cur != nil && *cur != "" {
		var page struct {
			Items []T     `json:"items"`
			Next  *string `json:"next"`
		}
		q := "sync/" + v.SessionID + "/plan?section=" + name + "&cursor=" + *cur
		if err := c.call("GET", q, nil, &page); err != nil {
			return nil, err
		}
		items = append(items, page.Items...)
		cur = page.Next
	}
	return items, nil
}

// download fetches one file into <path>.part (resuming), checks size + SHA-256, fsyncs and renames it in place.
func (c *Client) download(it item) (bool, error) {
	dest, err := c.target(it.Path)
	if err != nil {
		return false, err
	}
	part := dest + ".part"
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		return false, err
	}
	var have int64
	if fi, err := os.Stat(part); err == nil {
		have = fi.Size()
	}
	req, _ := http.NewRequest("GET", c.url("files/"+it.Fid), nil)
	req.Header.Set("Authorization", "Bearer "+c.st.Token)
	if have > 0 {
		req.Header.Set("Range", fmt.Sprintf("bytes=%d-", have))
		req.Header.Set("If-Range", `"`+it.Fid+`"`)
	}
	r, err := c.http.Do(req)
	if err != nil {
		return false, err
	}
	defer r.Body.Close()
	if r.StatusCode >= 400 {
		return false, &ProtoError{r.StatusCode, "download", it.Path}
	}
	flags := os.O_CREATE | os.O_WRONLY | os.O_TRUNC
	if r.StatusCode == 206 {
		flags = os.O_CREATE | os.O_WRONLY | os.O_APPEND
	}
	f, err := os.OpenFile(part, flags, 0o644)
	if err != nil {
		return false, err
	}
	c.pr.update(func(p *Progress) { p.File, p.FileSize, p.Bytes = cardPath(it.Path), it.Size, p.Bytes+have })
	_, err = io.Copy(io.MultiWriter(f, countWriter{&c.pr}), r.Body)
	if err == nil {
		err = f.Sync()
	}
	f.Close()
	if err != nil {
		return false, err
	}
	g, err := os.Open(part)
	if err != nil {
		return false, err
	}
	h := sha256.New()
	n, _ := io.Copy(h, g)
	g.Close()
	if n != it.Size || hex.EncodeToString(h.Sum(nil)) != it.Fid {
		os.Remove(part)
		return false, nil
	}
	if err := os.Rename(part, dest); err != nil {
		return false, err
	}
	return true, setMtime(dest, it.Mtime)
}

type Result struct {
	Fetched, Deleted, Failed int
	Cancelled                bool
	Sent                     map[string]int  // events the server took, by type (play, favourite, position)
	State                    []Wanted        // the library's hearts and play counts of the items on the card
	SentHearts               map[string]bool // keys whose heart or stars the player just sent up
	SentPositions            map[string]bool // keys whose position the player just sent up
	Generation               int             // tagcache generation installed from the server's set (0 = none)
	AfterSync                string          // what the server asks for after a sync that changed something
}

// Sync syncs until the server has nothing more to send: a large sync comes in windows (the server prepares only
// part of it at a time), each one a session of its own.
func (c *Client) Sync() (Result, error) {
	res, more, err := c.syncOnce()
	for err == nil && more && !res.Cancelled && res.Failed == 0 {
		var next Result
		next, more, err = c.syncOnce()
		res.Fetched += next.Fetched
		res.Deleted += next.Deleted
		res.Failed += next.Failed
		res.Cancelled = next.Cancelled
		res.State = next.State
		for k, v := range next.Sent {
			res.Sent[k] += v
		}
		for k := range next.SentHearts {
			res.SentHearts[k] = true
		}
		for k := range next.SentPositions {
			res.SentPositions[k] = true
		}
		if next.Generation > 0 {
			res.Generation = next.Generation
		}
		if next.AfterSync != "" {
			res.AfterSync = next.AfterSync
		}
	}
	return res, err
}

func (c *Client) syncOnce() (Result, bool, error) {
	res := Result{Sent: map[string]int{}, SentHearts: map[string]bool{}, SentPositions: map[string]bool{}}
	if _, err := c.collect(); err != nil {
		fmt.Fprintln(os.Stderr, "listening data not read:", err)
	}
	c.pr.update(func(p *Progress) { *p = Progress{Phase: phaseSend, Sent: map[string]int{}} })
	c.collectPlaylists()
	for _, e := range c.st.Events {
		if e["type"] == "favourite" || e["type"] == "rating" {
			k, _ := e["key"].(string)
			res.SentHearts[k] = true
		}
		if e["type"] == "position" {
			k, _ := e["key"].(string)
			res.SentPositions[k] = true
		}
	}
	if err := c.SendEvents(res.Sent); err != nil {
		return res, false, err
	}
	c.pr.update(func(p *Progress) {
		p.Phase = phasePlan
		for k, v := range res.Sent {
			p.Sent[k] = v
		}
	})
	c.progress(0, 0, "Asking the server what to sync")
	v, err := c.start()
	if err != nil {
		return res, false, err
	}
	dels, err := section(c, v, "delete", v.Plan.Delete)
	if err != nil {
		return res, false, err
	}
	fetch, err := section(c, v, "fetch", v.Plan.Fetch)
	if err != nil {
		return res, false, err
	}
	if res.State, err = section(c, v, "state", v.Plan.State); err != nil {
		return res, false, err
	}
	var total int64
	for _, it := range fetch {
		total += it.Size
	}
	c.pr.update(func(p *Progress) { p.Phase, p.FilesTotal, p.BytesTotal = phaseRemove, len(fetch), total })
	gone := []string{}
	for _, d := range dels {
		if cp := cardPath(d.Path); c.editedSince(cp) {
			continue // edited on the player after the last look: kept, the edit goes up next time
		}
		if t, err := c.target(d.Path); err == nil {
			os.Remove(t)
			delete(c.st.Lists, cardPath(d.Path))
			c.forget(c.st.Keys[d.Path]) // the server removed it: its playlist entries are not a user edit
			gone = append(gone, d.Path)
			delete(c.st.Files, d.Path)
			delete(c.st.Keys, d.Path)
			delete(c.st.Episodes, d.Path)
		}
	}
	if len(gone) > 0 {
		if err := c.call("POST", "sync/"+v.SessionID+"/ack", map[string]any{"deleted": gone}, nil); err != nil {
			return res, false, err
		}
	}
	res.Deleted = len(gone)
	c.pr.update(func(p *Progress) { p.Phase, p.Removed, p.Started = phaseDownload, len(gone), time.Now() })
	lists := []string{}
	stopped := false // the server stopped the sync (Cancel on its device page): its ack answer says so
	for i, it := range fetch {
		if stopped || (c.cancel != nil && c.cancel()) {
			res.Failed += len(fetch) - i
			res.Cancelled = true
			break
		}
		c.progress(i, len(fetch), it.Path)
		if c.editedSince(cardPath(it.Path)) {
			res.Failed++ // never overwrite a playlist edit the server has not taken yet
			continue
		}
		ok, err := c.download(it)
		if err != nil {
			return res, false, err
		}
		if !ok {
			res.Failed++
			continue
		}
		var ack struct {
			Cancel bool `json:"cancel"`
		}
		if err := c.call("POST", "sync/"+v.SessionID+"/ack", map[string]any{"done": []map[string]string{{"path": it.Path, "fid": it.Fid}}}, &ack); err != nil {
			return res, false, err
		}
		stopped = ack.Cancel
		c.st.Files[it.Path] = it.Size
		if it.Key != "" {
			c.st.Keys[it.Path] = it.Key
		}
		if it.Meta != nil && it.Meta.Kind == "podcast" {
			if c.st.Episodes == nil {
				c.st.Episodes = map[string]Meta{}
			}
			c.st.Episodes[it.Path] = *it.Meta
			c.ignorePodcasts()
		}
		res.Fetched++
		isList := strings.HasPrefix(cardPath(it.Path), "Playlists/")
		c.pr.update(func(p *Progress) {
			p.Files, p.Received = i+1, res.Fetched
			if isList {
				p.Playlists++
			}
		})
		if cp := cardPath(it.Path); strings.HasPrefix(cp, "Playlists/") {
			lists = append(lists, cp)
		}
		if res.Fetched%20 == 0 {
			_ = c.save() // a killed app keeps what it got
		}
	}
	for _, cp := range lists { // after the loop: the keys of tracks fetched later are known now
		if keys, ok := c.listKeys(cp); ok {
			c.st.Lists[cp] = keys
		}
	}
	res.AfterSync = v.Plan.AfterSync
	status := "complete"
	if res.Cancelled {
		status = "cancelled"
	} else if res.Failed > 0 {
		status = "partial"
	} else if v.Plan.Generated != nil && v.Plan.More == 0 {
		// only over a complete card (after the last window): the set describes every file of the plan
		c.pr.update(func(p *Progress) { p.Phase = phaseDatabase })
		c.progress(len(fetch), len(fetch), "Updating the player's library")
		if res.Generation, err = c.installGenerated(*v.Plan.Generated); err != nil {
			return res, false, err
		}
	}
	var fin struct {
		More bool `json:"more"`
	}
	if err := c.call("POST", "sync/"+v.SessionID+"/finish", map[string]any{"status": status}, &fin); err != nil {
		return res, false, err
	}
	c.progress(len(fetch), len(fetch), "Done")
	return res, fin.More, c.save()
}

func (c *Client) Record(typ, key string, fields map[string]any) error {
	c.queue(typ, key, fields)
	return c.save()
}

func (c *Client) queue(typ, key string, fields map[string]any) {
	c.queueAt(typ, key, time.Now(), fields)
}

func (c *Client) queueAt(typ, key string, at time.Time, fields map[string]any) {
	c.st.Seq++
	ev := map[string]any{"eid": uuid4(), "seq": c.st.Seq, "type": typ, "key": key,
		"at": at.UTC().Format(time.RFC3339), "clock": "ntp"}
	for k, v := range fields {
		ev[k] = v
	}
	if key == "" {
		delete(ev, "key")
	}
	c.st.Events = append(c.st.Events, ev)
}

// SendEvents sends the queued events; `sent` (may be nil) counts the ones the server took, by type.
func (c *Client) SendEvents(sent map[string]int) error {
	for len(c.st.Events) > 0 {
		n := len(c.st.Events)
		if n > 1000 {
			n = 1000
		}
		var out struct {
			Acked int `json:"acked_seq"`
		}
		if err := c.call("POST", "events", map[string]any{"events": c.st.Events[:n]}, &out); err != nil {
			return err
		}
		keep := []map[string]any{}
		for _, e := range c.st.Events {
			if int(toF(e["seq"])) > out.Acked {
				keep = append(keep, e)
			} else if sent != nil {
				t, _ := e["type"].(string)
				sent[t]++
			}
		}
		c.st.Events = keep
		if err := c.save(); err != nil {
			return err
		}
	}
	return nil
}

func toF(v any) float64 {
	switch x := v.(type) {
	case float64:
		return x
	case int:
		return float64(x)
	}
	return 0
}

func main() {
	if len(os.Args) == 1 {
		fpuFallback(runtime.GOARCH)
		openStartupLog() // launched from the player
	}
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}

func run(args []string) error {
	if len(args) == 0 {
		return runApp() // launched from the player: the full-screen UI
	}
	if args[0] == "screen" && len(args) == 4 {
		return screenDemo(args[1:])
	}
	c, err := open()
	if err != nil {
		return err
	}
	out := func(v any) { b, _ := json.Marshal(v); fmt.Println(string(b)) }
	switch args[0] {
	case "pair":
		if len(args) < 3 {
			return errors.New("usage: pair <server> <code> [name]")
		}
		name := "HiBy R1 - Compás"
		if len(args) > 3 {
			name = args[3]
		}
		if err := c.Pair(args[1], args[2], name); err != nil {
			return err
		}
		out(map[string]string{"device_id": c.st.DeviceID})
	case "sync":
		c.progress = func(d, t int, w string) { fmt.Fprintf(os.Stderr, "[%d/%d] %s\n", d, t, w) }
		r, err := c.Sync()
		if err != nil {
			return err
		}
		d, err := c.down(r.State, r.SentHearts, r.SentPositions)
		if err != nil {
			fmt.Fprintln(os.Stderr, "tagcache not read:", err)
		}
		if err := c.apply(d); err != nil { // headless (tests): no restart
			return err
		}
		out(map[string]any{"fetched": r.Fetched, "deleted": r.Deleted, "failed": r.Failed, "sent": r.Sent,
			"to_player": len(d.patches), "positions_down": len(d.positions), "generation": r.Generation, "after_sync": r.AfterSync})
	case "collect":
		n, err := c.collect()
		if err != nil {
			return err
		}
		out(map[string]int{"queued": n})
	case "installdb": // installdb <dir with a built tagcache>: install it as the card's next generation
		if len(args) != 2 {
			return errors.New("usage: installdb <dir>")
		}
		gen, err := installTagcache(c.root, args[1])
		if err != nil {
			return err
		}
		fmt.Printf("{\"generation\": %d, \"foreign\": %d}\n", gen, c.foreignAudio())
		return nil
	case "keys":
		out(c.st.Keys)
	case "play", "favourite", "position":
		if len(args) < 2 {
			return errors.New("usage: " + args[0] + " <key> ...")
		}
		f := map[string]any{}
		switch args[0] {
		case "play":
			f["completed"] = true
		case "favourite":
			f["on"] = true
		case "position":
			if len(args) < 3 {
				return errors.New("usage: position <key> <seconds>")
			}
			s, err := strconv.ParseFloat(args[2], 64)
			if err != nil {
				return err
			}
			f["position_s"], f["finished"] = s, false
		}
		if err := c.Record(args[0], args[1], f); err != nil {
			return err
		}
		out(map[string]int{"queued": len(c.st.Events)})
	default:
		return fmt.Errorf("unknown command %q", args[0])
	}
	return nil
}

// ignorePodcasts marks Podcasts/ with database.ignore (as the USB sync does): the player indexes every other
// folder as music, and the episodes belong to the Podcasts plugin.
func (c *Client) ignorePodcasts() {
	p := filepath.Join(c.root, "Podcasts", "database.ignore")
	if _, err := os.Stat(p); err == nil {
		return
	}
	_ = os.MkdirAll(filepath.Dir(p), 0o755)
	if f, err := os.Create(p); err == nil {
		f.Close()
	}
}
