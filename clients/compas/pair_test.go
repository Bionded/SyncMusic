// SPDX-License-Identifier: GPL-3.0-or-later
package main

import (
	"encoding/binary"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func dnsLabels(name string) []byte {
	var b []byte
	for _, l := range strings.Split(name, ".") {
		b = append(b, byte(len(l)))
		b = append(b, l...)
	}
	return append(b, 0)
}

func rr(name []byte, typ uint16, rdata []byte) []byte {
	b := append([]byte{}, name...)
	b = binary.BigEndian.AppendUint16(b, typ)
	b = binary.BigEndian.AppendUint16(b, 1)
	b = binary.BigEndian.AppendUint32(b, 120)
	b = binary.BigEndian.AppendUint16(b, uint16(len(rdata)))
	return append(b, rdata...)
}

// a reply as a SyncMusic server sends it: PTR → instance, SRV, TXT, A; the PTR target is compressed.
func mdnsReply() []byte {
	msg := make([]byte, 12)
	binary.BigEndian.PutUint16(msg[2:], 0x8400)
	binary.BigEndian.PutUint16(msg[6:], 4)
	svcOff := len(msg)
	inst := append([]byte{byte(len("Домашній (abc123)"))}, "Домашній (abc123)"...)
	inst = append(inst, 0xc0|byte(svcOff>>8), byte(svcOff))
	msg = append(msg, rr(dnsLabels(mdnsService), 12, inst)...)
	instName := dnsLabels("Домашній (abc123)." + mdnsService)
	srv := []byte{0, 0, 0, 0, 0x1f, 0x90}
	srv = append(srv, dnsLabels("nas.local")...)
	msg = append(msg, rr(instName, 33, srv)...)
	var txt []byte
	for _, kv := range []string{"v=1", "id=abc123", "name=Домашній", "path=/", "tls=0", "pair=1"} {
		txt = append(txt, byte(len(kv)))
		txt = append(txt, kv...)
	}
	msg = append(msg, rr(instName, 16, txt)...)
	return append(msg, rr(dnsLabels("nas.local"), 1, []byte{192, 0, 2, 7})...)
}

func TestDiscoverParsesTheServersAnswer(t *testing.T) {
	conn, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	go func() {
		buf := make([]byte, 1500)
		for {
			n, from, err := conn.ReadFromUDP(buf)
			if err != nil {
				return
			}
			if q := buf[:n]; strings.Contains(string(q), "_syncmusic") && q[n-2]&0x80 != 0 {
				_, _ = conn.WriteToUDP(mdnsReply(), from)
			}
		}
	}()
	t.Setenv("SYNCMUSIC_MDNS", conn.LocalAddr().String())
	list, err := discover(600 * time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}
	if len(list) != 1 || list[0] != (found{"Домашній", "http://192.0.2.7:8080/", true}) {
		t.Fatalf("got %+v", list)
	}
}

func TestPairingRequestPendingThenApproved(t *testing.T) {
	polls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == "POST" && r.URL.Path == "/api/client/v1/pair-requests":
			var body map[string]map[string]any
			_ = json.NewDecoder(r.Body).Decode(&body)
			if body["client"]["os"] != "Compas" {
				t.Errorf("client info: %v", body)
			}
			w.WriteHeader(202)
			_, _ = w.Write([]byte(`{"request_id":"r1","secret":"s3","display_code":"4821","poll_s":1}`))
		case r.URL.Path == "/api/client/v1/pair-requests/r1":
			if r.Header.Get("X-Pair-Secret") != "s3" {
				w.WriteHeader(410)
				return
			}
			if polls++; polls < 2 {
				w.WriteHeader(202)
				_, _ = w.Write([]byte(`{"status":"pending","poll_s":1}`))
				return
			}
			_, _ = w.Write([]byte(`{"status":"approved","device_id":"dev9","token":"smc_x"}`))
		case r.URL.Path == "/api/client/v1/pair-requests/r2":
			w.WriteHeader(403)
			_, _ = w.Write([]byte(`{"error":{"code":"request_rejected","message":"no"}}`))
		}
	}))
	defer srv.Close()
	c := &Client{data: t.TempDir(), http: srv.Client(), st: State{UID: "u"}}
	q, err := c.RequestPair(strings.TrimPrefix(srv.URL, "http://"), playerName) // no scheme: http
	if err != nil || q.DisplayCode != "4821" || c.st.Server != srv.URL+"/" {
		t.Fatalf("%+v %v %s", q, err, c.st.Server)
	}
	if ok, err := c.PollPair(q); ok || err != nil {
		t.Fatalf("pending: %v %v", ok, err)
	}
	if ok, err := c.PollPair(q); !ok || err != nil || c.st.Token != "smc_x" || c.st.DeviceID != "dev9" {
		t.Fatalf("approved: %v %v %+v", ok, err, c.st)
	}
	_, err = c.PollPair(pairAsk{ID: "r2", Secret: "s"})
	if pe, ok := err.(*ProtoError); !ok || pe.Code != "request_rejected" {
		t.Fatalf("rejected: %v", err)
	}
}

func TestForgetKeepsTheMusic(t *testing.T) {
	c := &Client{data: t.TempDir(), st: State{Server: "http://x/", Token: "t", DeviceID: "d",
		Files: map[string]int64{"Music/a.mp3": 3}, Events: []map[string]any{{"type": "play"}}}}
	if err := c.Forget(); err != nil {
		t.Fatal(err)
	}
	if c.st.Token != "" || c.st.Server != "" || len(c.st.Events) != 0 || c.st.Files["Music/a.mp3"] != 3 {
		t.Fatalf("%+v", c.st)
	}
}

func TestKeyboardTypesDeletesAndConfirms(t *testing.T) {
	got := ""
	a := &App{}
	kb := a.keypad("Code", func(s string) { got = s })
	press := func(label string) {
		for _, b := range kb().buttons {
			if b.label == label {
				b.act()
				return
			}
		}
		t.Fatalf("no key %q", label)
	}
	for _, k := range []string{"1", "2", "7", "⌫", "3", "4", "5", "6", "7", "8", "9", "0"} { // 8 digits at most
		press(k)
	}
	if p := kb(); p.field != "1234-5678" || p.grid != 12 || p.cols != 3 {
		t.Fatalf("field %q grid %d", p.field, p.grid)
	}
	labels := []string{}
	for _, b := range kb().buttons[:12] {
		labels = append(labels, b.label)
	}
	if strings.Join(labels, " ") != "1 2 3 4 5 6 7 8 9 ⌫ 0 Connect" { // phone numpad order
		t.Fatalf("keys %v", labels)
	}
	press("Connect")
	if got != "12345678" {
		t.Fatalf("confirmed %q", got)
	}
	if normalizeServer("192.0.2.5:8080") != "http://192.0.2.5:8080/" || normalizeServer("https://h/") != "https://h/" {
		t.Fatal("normalizeServer")
	}
}

func TestPreviewConfirmAndDecline(t *testing.T) {
	calls := []string{}
	reviewed := false
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls = append(calls, r.Method+" "+strings.TrimPrefix(r.URL.Path, "/api/client/v1/"))
		switch {
		case r.Method == "POST" && strings.HasSuffix(r.URL.Path, "/sync"):
			if !reviewed {
				reviewed = true
				_, _ = w.Write([]byte(`{"session_id":"s1","status":"needs_review","message":"removes 5 of 9 items",
					"may_confirm":true,"review":{"fetch_count":2,"fetch_bytes":9000000,"delete_count":5},"retry_after_s":60}`))
				return
			}
			fallthrough
		case strings.HasSuffix(r.URL.Path, "/confirm"):
			_, _ = w.Write([]byte(`{"session_id":"s1","status":"ready","plan":{"totals":{"fetch_count":1,"fetch_bytes":10},
				"fetch":[{"path":"Playlists/Mix.m3u8","size":10}]}}`))
		default:
			_, _ = w.Write([]byte(`{}`))
		}
	}))
	defer srv.Close()
	c := &Client{data: t.TempDir(), root: t.TempDir(), http: srv.Client(), st: State{Server: srv.URL + "/", Token: "t"},
		progress: func(int, int, string) {}}
	seen := []Preview{}
	answers := []bool{true, false} // confirm the review, then decline the plan
	c.preview = func(p Preview) bool {
		seen = append(seen, p)
		ok := answers[0]
		answers = answers[1:]
		return ok
	}
	if _, err := c.start(); err != errDeclined {
		t.Fatalf("declined: %v", err)
	}
	if len(seen) != 2 || !seen[0].Confirm || seen[0].DeleteCount != 5 || seen[0].Reason != "removes 5 of 9 items" ||
		seen[1].Confirm || seen[1].Playlists != 1 {
		t.Fatalf("previews %+v", seen)
	}
	want := []string{"POST sync", "POST sync/s1/confirm", "POST sync/s1/finish"}
	if strings.Join(calls, ",") != strings.Join(want, ",") {
		t.Fatalf("calls %v", calls)
	}
}

func TestDiagnosticsLeaveTheTokenOut(t *testing.T) {
	root := t.TempDir()
	_ = os.MkdirAll(filepath.Join(root, ".apps"), 0o755)
	_ = os.WriteFile(filepath.Join(root, ".apps", ".last-session.log"), []byte("hello smc_madeup0000 bye\n"), 0o644)
	var got map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/client/v1/diagnostics" {
			_ = json.NewDecoder(r.Body).Decode(&got)
		}
		w.WriteHeader(202)
	}))
	defer srv.Close()
	tok := "smc_" + "madeup0000" // made up
	c := &Client{root: root, http: srv.Client(), st: State{Server: srv.URL + "/", Token: tok, DeviceID: "d1",
		Files: map[string]int64{"a": 1}}}
	if err := c.SendDiagnostics(defaultSettings()); err != nil {
		t.Fatal(err)
	}
	b, _ := json.Marshal(got)
	if strings.Contains(string(b), "madeup0000") || !strings.Contains(string(b), "hello [redacted] bye") ||
		got["summary"].(map[string]any)["files"].(float64) != 1 {
		t.Fatalf("%s", b)
	}
}

func TestTemplatesSwitch(t *testing.T) {
	var put []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == "PUT" {
			var b struct{ On []string }
			_ = json.NewDecoder(r.Body).Decode(&b)
			put = b.On
		}
		_, _ = w.Write([]byte(`{"templates":[{"id":"a","name":"Рок","on":true},{"id":"b","name":"Books","on":false}]}`))
	}))
	defer srv.Close()
	c := &Client{http: srv.Client(), st: State{Server: srv.URL + "/", Token: "t"}}
	l, err := c.Templates()
	if err != nil || len(l) != 2 || !l[0].On || l[1].Name != "Books" {
		t.Fatalf("%v %v", l, err)
	}
	if _, err := c.SetTemplates([]string{"b"}); err != nil || len(put) != 1 || put[0] != "b" {
		t.Fatalf("%v %v", put, err)
	}
}
