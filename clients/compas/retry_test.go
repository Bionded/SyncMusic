// SPDX-License-Identifier: GPL-3.0-or-later
package main

import (
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

// a server that drops the connection without an answer (a stale keep-alive connection: EOF)
func flakyServer(t *testing.T, drops int32) (*httptest.Server, *int32) {
	var n int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if atomic.AddInt32(&n, 1) <= drops {
			conn, _, err := w.(http.Hijacker).Hijack()
			if err == nil {
				conn.Close()
			}
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"acked_seq": 3}`))
	}))
	t.Cleanup(srv.Close)
	return srv, &n
}

func TestAckRetriedAfterEOF(t *testing.T) {
	old := retryDelays
	retryDelays = []time.Duration{time.Millisecond, time.Millisecond, time.Millisecond}
	defer func() { retryDelays = old }()
	srv, n := flakyServer(t, 2)
	c := &Client{st: State{Server: srv.URL, Token: "t"}, http: newHTTPClient(0)}
	if err := c.call("POST", "sync/abc/ack", map[string]any{"done": []any{}}, nil); err != nil {
		t.Fatalf("ack after two dropped connections: %v", err)
	}
	if *n != 3 {
		t.Fatalf("tries = %d, want 3", *n)
	}
}

func TestSyncStartNotRetried(t *testing.T) {
	srv, n := flakyServer(t, 1)
	c := &Client{st: State{Server: srv.URL}, http: newHTTPClient(0)}
	if err := c.call("POST", "sync", map[string]any{}, nil); err == nil {
		t.Fatal("a dropped sync start must not be sent again")
	}
	if *n != 1 {
		t.Fatalf("tries = %d, want 1", *n)
	}
}

func TestRetryableCalls(t *testing.T) {
	for p, want := range map[string]bool{"sync/x/ack": true, "sync/x/finish": true, "events": true,
		"sync": false, "pair": false, "pair-requests": false, "diagnostics": false} {
		if retryable("POST", p) != want {
			t.Errorf("retryable(POST %s) != %v", p, want)
		}
	}
}

// TestUpdateOfferedOnConnect: a newer app on the server opens the Update/Later prompt from Home; Later tells the
// server and goes back Home (the button stays there); an app newer than the server only gets a notice.
func TestUpdateOfferedOnConnect(t *testing.T) {
	var answers []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		answers = append(answers, r.URL.Path)
	}))
	defer srv.Close()
	a := &App{c: &Client{root: t.TempDir(), st: State{Server: srv.URL, Token: "t"}, http: newHTTPClient(0)},
		set: defaultSettings(), later: make(chan func(), 1)}
	a.online.Store(ptr(T("home.online")))
	a.go_(a.home)
	a.offerUpdate(appUpdate{Available: true, Version: "9.9.9", ServerVersion: "9.9.9"})
	p := a.build()
	if p.title != T("update.title") || p.buttons[0].label != T("btn.update_now") || p.buttons[1].label != T("btn.later") {
		t.Fatalf("no update prompt: %+v", p)
	}
	p.buttons[1].act()
	if a.build().title != T("home.title") || len(answers) != 1 || answers[0] != "/api/client/v1/app-update/answer" {
		t.Fatalf("Later: page %q, answers %v", a.build().title, answers)
	}
	b := &App{c: a.c, set: defaultSettings(), later: make(chan func(), 1)}
	b.online.Store(ptr(T("home.online")))
	b.go_(b.home)
	b.offerUpdate(appUpdate{ServerVersion: "0.0.1", ClientNewer: true})
	h := b.build()
	if h.title != T("home.title") || h.lines[len(h.lines)-1].text == "" {
		t.Fatal("a newer app must stay on Home")
	}
	found := false
	for _, l := range h.lines {
		found = found || l.text == T("home.newer", version, "0.0.1")
	}
	if !found {
		t.Fatal("no 'newer than the server' notice on Home")
	}
}

// TestPowerKeyTogglesTheScreen: a short Power press switches the screen off and on; a long press does nothing.
func TestPowerKeyTogglesTheScreen(t *testing.T) {
	fakeSys(t, nil)
	a := &App{bl: &backlight{}, set: defaultSettings(), mode: screenOn}
	a.build = func() page { return page{} }
	t0 := time.Now()
	press := func(at time.Time, hold time.Duration) {
		a.handle(inEv{code: keyPower, down: true}, at)
		a.handle(inEv{code: keyPower, down: false}, at.Add(hold))
	}
	press(t0, 200*time.Millisecond)
	if a.mode != screenOff {
		t.Fatal("a short press must switch the screen off")
	}
	press(t0.Add(time.Second), 200*time.Millisecond)
	if a.mode != screenOn {
		t.Fatal("a second short press must switch it on")
	}
	press(t0.Add(2*time.Second), 3*time.Second)
	if a.mode != screenOn {
		t.Fatal("a long press must not act (the player powers off)")
	}
}

// TestUpdateOfferedRightAfterRegistration: once the server says the registration is finished, the app asks for an
// update at once and opens the prompt over the "Paired" page, before the first sync.
func TestUpdateOfferedRightAfterRegistration(t *testing.T) {
	old := version
	version = "1.0.0"
	defer func() { version = old }()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/api/client/v1/status":
			_, _ = w.Write([]byte(`{"registration": "done"}`))
		case "/api/client/v1/app-update":
			_, _ = w.Write([]byte(`{"available": true, "version": "9.9.9", "server_version": "9.9.9",
				"files": [{"path": "syncmusic-compas", "size": 1, "sha256": "x", "url": "u"}]}`))
		}
	}))
	defer srv.Close()
	a := &App{c: &Client{root: t.TempDir(), st: State{Server: srv.URL, Token: "t"}, http: newHTTPClient(0)},
		set: defaultSettings(), later: make(chan func(), 4)}
	a.waitRegistration(a.message(T("pair.done"), T("pair.done_text"), false))
	deadline := time.After(5 * time.Second)
	for a.build().title != T("update.title") {
		select {
		case f := <-a.later:
			f()
		case <-deadline:
			t.Fatalf("no update prompt after registration, page %q", a.build().title)
		}
	}
}

// TestServerSyncReachesAFreshlyPairedApp: right after pairing (no restart) "Sync" pressed on the server starts the
// sync in the app, which says the server started it.
func TestServerSyncReachesAFreshlyPairedApp(t *testing.T) {
	fakeSys(t, nil)
	var asked int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Path == "/api/client/v1/status" && r.URL.Query().Get("wait") != "" {
			atomic.AddInt32(&asked, 1)
			_, _ = w.Write([]byte(`{"requested": true}`)) // stays requested until the app picks it up
			return
		}
		_, _ = w.Write([]byte(`{"registration": "done"}`))
	}))
	defer srv.Close()
	defer srv.CloseClientConnections() // the sync it started still talks to the server
	a := &App{c: &Client{root: t.TempDir(), data: t.TempDir(), st: State{Server: srv.URL, Token: "t"}, http: newHTTPClient(0)},
		set: defaultSettings(), later: make(chan func(), 8)}
	a.paired()
	deadline := time.After(10 * time.Second)
	for !a.syncing {
		select {
		case f := <-a.later:
			f()
		case <-deadline:
			t.Fatalf("server sync never started, page %q", a.build().title)
		}
	}
	if a.pwhat != T("sync.from_server") || atomic.LoadInt32(&asked) == 0 {
		t.Fatalf("sync page says %q", a.pwhat)
	}
}
