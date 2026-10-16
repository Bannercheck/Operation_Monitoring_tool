package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func writeExport(t *testing.T, dir string, d updExport) {
	t.Helper()
	b, _ := json.Marshal(d)
	if err := os.WriteFile(filepath.Join(dir, "agent-update.json"), b, 0o640); err != nil {
		t.Fatal(err)
	}
}

func getJSON(t *testing.T, url, tok string, hdr map[string]string) (int, map[string]any) {
	t.Helper()
	req, _ := http.NewRequest("GET", url, nil)
	if tok != "" {
		req.Header.Set("Authorization", "Bearer "+tok)
	}
	for k, v := range hdr {
		req.Header.Set(k, v)
	}
	r, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer r.Body.Close()
	var m map[string]any
	_ = json.NewDecoder(r.Body).Decode(&m)
	return r.StatusCode, m
}

func TestRolloutBucketMatchesTheDashboard(t *testing.T) {
	// the same numbers src/watchover/agent_update.py computes: int(sha256(f"{id}:{ver}")[:8], 16) % 100
	for _, c := range []struct {
		id   int64
		v    string
		want int
	}{{7, "0.9.0", 2}, {1, "0.8.0", 82}, {42, "1.2.3", 76}} {
		if got := bucket(c.id, c.v); got != c.want {
			t.Errorf("bucket(%d,%s)=%d want %d", c.id, c.v, got, c.want)
		}
	}
	if !newer("0.10.0", "0.9.9") || newer("0.9.0", "0.9.0") || newer("1.0.0-rc1", "1.0.0") || !newer("1.0.0", "1.0.0-rc1") {
		t.Fatal("newer")
	}
}

func TestDecidePolicy(t *testing.T) {
	dir := t.TempDir()
	r := newReleases(dir, dir, "")
	if d := r.decide(1, "wo-agent", "0.8.0"); d.Update || d.Reason != "unsigned" {
		t.Fatalf("no export: %+v", d)
	}
	rel := &signedDoc{Body: "Ym9keQ==", Sig: "c2ln", Key: "ed25519:x", Version: "0.9.0"}
	writeExport(t, dir, updExport{Release: rel, Enabled: true, Pct: 100, Paused: []int64{3},
		Pins: map[string]signedDoc{"4": {Body: "cGlu", Sig: "c2ln", Version: "0.8.0"}}})
	r.checked = time.Time{}
	cases := []struct {
		id         int64
		eng, ver   string
		update     bool
		reason, to string
	}{
		{1, "wo-agent", "0.8.0", true, "newer", "0.9.0"},
		{1, "wo-agent", "0.9.0", false, "current", "0.9.0"},
		{1, "wo-agent", "1.0.0", false, "current", "0.9.0"}, // never offered a downgrade
		{1, "agent.py", "0.6.0", true, "migrate", "0.9.0"},
		{3, "wo-agent", "0.8.0", false, "paused", "0.9.0"},
		{4, "wo-agent", "0.9.0", true, "pinned", "0.8.0"},
		{4, "wo-agent", "0.8.0", false, "pinned", "0.8.0"},
	}
	for _, c := range cases {
		d := r.decide(c.id, c.eng, c.ver)
		if d.Update != c.update || d.Reason != c.reason || d.Version != c.to || d.Release == nil {
			t.Errorf("%+v: got %+v", c, d)
		}
		if c.id == 4 && d.Pin == nil {
			t.Error("pin not passed on")
		}
	}
	writeExport(t, dir, updExport{Release: rel, Enabled: true, Pct: 3})
	r.checked = time.Time{}
	// staged rollout: agent 7 is in bucket 2 (< 3 %), agent 1 in bucket 82
	if d := r.decide(7, "wo-agent", "0.8.0"); !d.Update {
		t.Errorf("bucket 2 at 3 %%: %+v", d)
	}
	if d := r.decide(1, "wo-agent", "0.8.0"); d.Update || d.Reason != "rollout" {
		t.Errorf("bucket 82 at 3 %%: %+v", d)
	}
	writeExport(t, dir, updExport{Release: rel, Enabled: false, Pct: 100})
	time.Sleep(10 * time.Millisecond)
	_ = os.Chtimes(filepath.Join(dir, "agent-update.json"), time.Now(), time.Now().Add(time.Second))
	r.checked = time.Time{}
	if d := r.decide(7, "wo-agent", "0.8.0"); d.Update || d.Reason != "disabled" {
		t.Errorf("switch off: %+v", d)
	}
}

func TestAgentUpdateEndpointHintAndSpoolHeaders(t *testing.T) {
	st := &authState{Agents: map[string]agentRec{sha("wo_good"): {ID: 7, Name: "app-07", Env: "prod"}}}
	s, ts, stop := setup(t, st, nil)
	defer stop()
	writeExport(t, s.cfg.spool, updExport{Release: &signedDoc{Body: "Ym9keQ==", Sig: "c2ln", Key: "ed25519:k", Fp: "SHA256:f", Version: "0.9.0"}, Enabled: true, Pct: 100})
	if code, _ := getJSON(t, ts.URL+"/agent/update", "", nil); code != 401 {
		t.Fatalf("no token: %d", code)
	}
	code, m := getJSON(t, ts.URL+"/agent/update", "wo_good", map[string]string{"X-Agent-Version": "0.8.0", "X-Agent-Engine": "wo-agent"})
	if code != 200 || m["update"] != true || m["version"] != "0.9.0" || m["agent"] != "app-07" || m["release"].(map[string]any)["sig"] != "c2ln" {
		t.Fatalf("decision %d %v", code, m)
	}
	// an /ingest answer carries the hint; the spool record carries the build and its update state
	req, _ := http.NewRequest("POST", ts.URL+"/ingest", strings.NewReader("hello\n"))
	req.Header.Set("Authorization", "Bearer wo_good")
	req.Header.Set("X-Agent-Version", "0.8.0")
	req.Header.Set("X-Agent-Engine", "wo-agent")
	req.Header.Set("X-Agent-Update", "rolled_back;0.9.0;0.8.0;2026-10-16T10:00:00Z;no hello")
	r, err := http.DefaultClient.Do(req)
	if err != nil || r.StatusCode != 200 || r.Header.Get("X-Watchover-Update") != "0.9.0" {
		t.Fatalf("ingest %v %v hint %q", err, r.StatusCode, r.Header.Get("X-Watchover-Update"))
	}
	r.Body.Close()
	req, _ = http.NewRequest("POST", ts.URL+"/ingest", strings.NewReader("x\n"))
	req.Header.Set("Authorization", "Bearer wo_good")
	req.Header.Set("X-Agent-Version", "0.9.0")
	req.Header.Set("User-Agent", "wo-agent/0.9.0") // an engine is told by X-Agent-Engine, else by the User-Agent
	r, _ = http.DefaultClient.Do(req)
	if r.Header.Get("X-Watchover-Update") != "" {
		t.Fatal("hint for an agent that is current")
	}
	r.Body.Close()
	var recs []map[string]any
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if recs = records(t, s.cfg.spool); len(recs) >= 2 {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	if len(recs) < 1 || recs[0]["ver"] != "0.8.0" || recs[0]["eng"] != "wo-agent" || !strings.HasPrefix(recs[0]["upd"].(string), "rolled_back;0.9.0") {
		t.Fatalf("spool headers %v", recs)
	}
}

func TestManifestPrefersSignedCopiesBootAndCA(t *testing.T) {
	s, ts, stop := setup(t, &authState{Open: true}, func(c *config) { c.agentCA = "/nonexistent.pem," + filepath.Join(c.spool, "edge-root.crt") })
	defer stop()
	_ = os.WriteFile(filepath.Join(s.cfg.agentDir, "wo-agent-linux-amd64"), []byte("image build"), 0o755)
	_ = os.MkdirAll(filepath.Join(s.cfg.spool, "release"), 0o755)
	signed := []byte("the build keyd hashed")
	_ = os.WriteFile(filepath.Join(s.cfg.spool, "release", "wo-agent-linux-amd64"), signed, 0o755)
	_ = os.WriteFile(filepath.Join(s.cfg.spool, "release", "agent-boot.sh"), []byte("#!/bin/bash\n# boot\n"), 0o644)
	writeExport(t, s.cfg.spool, updExport{Release: &signedDoc{Body: "Ym9keQ==", Sig: "c2ln", Key: "ed25519:k", Fp: "SHA256:f", Version: "0.9.0"}})
	r, _ := http.Get(ts.URL + "/agent/wo-agent-linux-amd64")
	b, _ := io.ReadAll(r.Body)
	r.Body.Close()
	if string(b) != string(signed) {
		t.Fatalf("served %q", b)
	}
	_, m := getJSON(t, ts.URL+"/agent/manifest.json", "", nil)
	h := sha256.Sum256(signed)
	f := m["files"].(map[string]any)
	if f["wo-agent-linux-amd64"].(map[string]any)["sha256"] != hex.EncodeToString(h[:]) || m["release_sig"] != "c2ln" || m["release_key"] != "ed25519:k" ||
		m["release_version"] != "0.9.0" || f["agent-boot.sh"] == nil {
		t.Fatalf("manifest %v", m)
	}
	r, _ = http.Get(ts.URL + "/agent/boot.sh")
	b, _ = io.ReadAll(r.Body)
	r.Body.Close()
	if r.StatusCode != 200 || !strings.Contains(string(b), "# boot") {
		t.Fatalf("boot.sh %d %q", r.StatusCode, b)
	}
	if r, _ := http.Get(ts.URL + "/agent/ca.crt"); r.StatusCode != 404 {
		t.Fatalf("no CA configured yet: %d", r.StatusCode)
	}
	pem := "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
	_ = os.WriteFile(filepath.Join(s.cfg.spool, "edge-root.crt"), []byte(pem), 0o644)
	r, _ = http.Get(ts.URL + "/agent/ca.crt")
	b, _ = io.ReadAll(r.Body)
	r.Body.Close()
	if r.StatusCode != 200 || string(b) != pem {
		t.Fatalf("ca.crt %d %q", r.StatusCode, b)
	}
	s.rel.exportCA()
	if got, _ := os.ReadFile(filepath.Join(s.cfg.spool, "agent-ca.pem")); string(got) != pem {
		t.Fatal("CA not exported for the dashboard")
	}
	_ = os.Remove(filepath.Join(s.cfg.spool, "edge-root.crt"))
	s.rel.exportCA()
	if fileExists(filepath.Join(s.cfg.spool, "agent-ca.pem")) {
		t.Fatal("stale CA export kept")
	}
}

// Two writes inside one timestamp tick (coarse mtimes: Docker Desktop's VM, overlay file systems) must still be seen.
func TestReleasesReloadSameMtime(t *testing.T) {
	dir := t.TempDir()
	r := newReleases(dir, dir, "")
	rel := &signedDoc{Body: "Ym9keQ==", Sig: "c2ln", Key: "ed25519:x", Version: "0.9.0"}
	p := filepath.Join(dir, "agent-update.json")
	writeExport(t, dir, updExport{Release: rel, Enabled: true, Pct: 100})
	fi, _ := os.Stat(p)
	if d := r.load(); d == nil || d.Pct != 100 {
		t.Fatalf("first load: %+v", d)
	}
	writeExport(t, dir, updExport{Release: rel, Enabled: true, Pct: 3})
	_ = os.Chtimes(p, fi.ModTime(), fi.ModTime()) // same mtime as before
	r.checked = time.Time{}
	if d := r.load(); d == nil || d.Pct != 3 {
		t.Fatalf("a rewrite with the same mtime was missed: %+v", d)
	}
}
