// SPDX-License-Identifier: GPL-3.0-or-later
// Finding and pairing a server: mDNS discovery of `_syncmusic._tcp.local.`, pairing requests approved on the
// server (no code), and forgetting the server (the music on the card stays).
package main

import (
	"encoding/binary"
	"errors"
	"fmt"
	"net"
	"os"
	"sort"
	"strings"
	"time"
)

const mdnsService = "_syncmusic._tcp.local"

type found struct {
	Name     string
	URL      string
	PairOpen bool // a pairing code is open on the server
}

// mdnsQuery is a PTR question for the service, asking for a unicast answer (we send from our own port).
func mdnsQuery() []byte {
	b := make([]byte, 12, 64)
	binary.BigEndian.PutUint16(b[4:], 1) // one question
	for _, l := range strings.Split(mdnsService, ".") {
		b = append(b, byte(len(l)))
		b = append(b, l...)
	}
	return append(b, 0, 0, 12, 0x80, 1) // root, type PTR, class IN with the unicast-response bit
}

// dnsName reads a (possibly compressed) name at off; returns the name and the offset after it.
func dnsName(msg []byte, off int) (string, int, error) {
	var labels []string
	end, jumps := -1, 0
	for {
		if off >= len(msg) {
			return "", 0, errors.New("short name")
		}
		n := int(msg[off])
		switch {
		case n == 0:
			if end < 0 {
				end = off + 1
			}
			return strings.Join(labels, "."), end, nil
		case n&0xc0 == 0xc0:
			if off+1 >= len(msg) || jumps > 20 {
				return "", 0, errors.New("bad pointer")
			}
			if end < 0 {
				end = off + 2
			}
			off, jumps = int(binary.BigEndian.Uint16(msg[off:])&0x3fff), jumps+1
		default:
			if off+1+n > len(msg) {
				return "", 0, errors.New("short label")
			}
			labels = append(labels, string(msg[off+1:off+1+n]))
			off += 1 + n
		}
	}
}

type mdnsRecords struct {
	ptr map[string]bool // service instance names
	srv map[string]struct {
		host string
		port int
	}
	txt  map[string]map[string]string // instance → key/value
	addr map[string]net.IP            // host → IPv4
}

func newRecords() *mdnsRecords {
	return &mdnsRecords{ptr: map[string]bool{}, srv: map[string]struct {
		host string
		port int
	}{}, txt: map[string]map[string]string{}, addr: map[string]net.IP{}}
}

// parse adds the records of one mDNS message (answers and additionals alike).
func (r *mdnsRecords) parse(msg []byte) error {
	if len(msg) < 12 {
		return errors.New("short message")
	}
	qd := int(binary.BigEndian.Uint16(msg[4:]))
	n := int(binary.BigEndian.Uint16(msg[6:])) + int(binary.BigEndian.Uint16(msg[8:])) + int(binary.BigEndian.Uint16(msg[10:]))
	off := 12
	for i := 0; i < qd; i++ {
		_, o, err := dnsName(msg, off)
		if err != nil {
			return err
		}
		off = o + 4
	}
	for i := 0; i < n; i++ {
		name, o, err := dnsName(msg, off)
		if err != nil || o+10 > len(msg) {
			return errors.New("bad record")
		}
		typ, rdlen := binary.BigEndian.Uint16(msg[o:]), int(binary.BigEndian.Uint16(msg[o+8:]))
		rd := o + 10
		if rd+rdlen > len(msg) {
			return errors.New("short record")
		}
		name = strings.ToLower(name)
		switch typ {
		case 12: // PTR
			if strings.EqualFold(name, mdnsService) {
				if inst, _, err := dnsName(msg, rd); err == nil {
					r.ptr[strings.ToLower(inst)] = true
				}
			}
		case 33: // SRV: priority, weight, port, target
			if rdlen > 6 {
				if host, _, err := dnsName(msg, rd+6); err == nil {
					r.srv[name] = struct {
						host string
						port int
					}{strings.ToLower(host), int(binary.BigEndian.Uint16(msg[rd+4:]))}
				}
			}
		case 16: // TXT
			kv := map[string]string{}
			for p := rd; p < rd+rdlen; {
				l := int(msg[p])
				if p+1+l > rd+rdlen {
					break
				}
				k, v, _ := strings.Cut(string(msg[p+1:p+1+l]), "=")
				kv[k] = v
				p += 1 + l
			}
			r.txt[name] = kv
		case 1: // A
			if rdlen == 4 {
				r.addr[name] = net.IP(msg[rd : rd+4])
			}
		}
		off = rd + rdlen
	}
	return nil
}

// servers: what the records describe, sorted by name.
func (r *mdnsRecords) servers() []found {
	var out []found
	for inst := range r.ptr {
		s, ok := r.srv[inst]
		ip := r.addr[s.host]
		if !ok || ip == nil {
			continue
		}
		t := r.txt[inst]
		scheme := "http"
		if t["tls"] == "1" {
			scheme = "https"
		}
		path := t["path"]
		if path == "" {
			path = "/"
		}
		name := t["name"]
		if name == "" {
			name = strings.TrimSuffix(inst, "."+mdnsService)
		}
		out = append(out, found{Name: name, URL: fmt.Sprintf("%s://%s:%d%s", scheme, ip, s.port, path), PairOpen: t["pair"] == "1"})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

// discover asks the LAN for SyncMusic servers for `wait` (`SYNCMUSIC_MDNS` = "host:port" to ask instead
// of the mDNS group, for tests).
func discover(wait time.Duration) ([]found, error) {
	to := "224.0.0.251:5353"
	if v := os.Getenv("SYNCMUSIC_MDNS"); v != "" {
		to = v
	}
	dst, err := net.ResolveUDPAddr("udp4", to)
	if err != nil {
		return nil, err
	}
	conn, err := net.ListenUDP("udp4", &net.UDPAddr{})
	if err != nil {
		return nil, err
	}
	defer conn.Close()
	recs := newRecords()
	buf := make([]byte, 9000)
	deadline := time.Now().Add(wait)
	for try := 0; try < 2; try++ { // a second query halfway: multicast gets lost
		if _, err := conn.WriteToUDP(mdnsQuery(), dst); err != nil {
			return nil, err
		}
		_ = conn.SetReadDeadline(time.Now().Add(wait / 2))
		for time.Now().Before(deadline) {
			n, _, err := conn.ReadFromUDP(buf)
			if err != nil {
				break
			}
			_ = recs.parse(buf[:n])
		}
	}
	return recs.servers(), nil
}

// normalizeServer: "192.0.2.5:8080" → "http://192.0.2.5:8080/".
func normalizeServer(s string) string {
	s = strings.TrimSpace(s)
	if !strings.Contains(s, "://") {
		s = "http://" + s
	}
	if !strings.HasSuffix(s, "/") {
		s += "/"
	}
	return s
}

type pairAsk struct {
	ID, Secret, DisplayCode string
	PollS                   int
}

func (c *Client) clientInfo(name string) map[string]any {
	return map[string]any{"uid": c.st.UID, "name": name, "os": "Compas", "app": "syncmusic-compas", "app_version": version}
}

// RequestPair asks the server to pair without a code; the user approves it there.
func (c *Client) RequestPair(server, name string) (pairAsk, error) {
	c.st.Server = normalizeServer(server)
	var out struct {
		RequestID   string `json:"request_id"`
		Secret      string `json:"secret"`
		DisplayCode string `json:"display_code"`
		PollS       int    `json:"poll_s"`
	}
	err := c.call("POST", "pair-requests", map[string]any{"client": c.clientInfo(name), "capabilities": c.capabilities()}, &out)
	return pairAsk{out.RequestID, out.Secret, out.DisplayCode, max(out.PollS, 1)}, err
}

// PollPair: true once approved (the token is saved); a rejected or expired request is a ProtoError.
func (c *Client) PollPair(q pairAsk) (bool, error) {
	var out struct {
		Status   string `json:"status"`
		Token    string `json:"token"`
		DeviceID string `json:"device_id"`
	}
	if err := c.callH("GET", "pair-requests/"+q.ID, map[string]string{"X-Pair-Secret": q.Secret}, nil, &out); err != nil {
		return false, err
	}
	if out.Status != "approved" || out.Token == "" {
		return false, nil
	}
	c.st.Token, c.st.DeviceID = out.Token, out.DeviceID
	return true, c.save()
}

// Forget drops the pairing (server, token, device id, unsent events); the files on the card stay.
func (c *Client) Forget() error {
	c.st.Server, c.st.Token, c.st.DeviceID = "", "", ""
	c.st.Events, c.st.LastSync, c.st.LastSummary = []map[string]any{}, "", ""
	return c.save()
}
