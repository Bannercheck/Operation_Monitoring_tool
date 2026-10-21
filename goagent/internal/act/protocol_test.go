package act

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// A fake dashboard speaking the job channel exactly as watchover.act.jobs does (§9.3.2–§9.3.5).
type fakeServer struct {
	t        *testing.T
	dispatch ed25519.PrivateKey
	anchor   ed25519.PrivateKey
	mu       sync.Mutex
	agentPub ed25519.PublicKey
	queue    []map[string]any
	results  map[string]map[string]any
	starts   []string
	hellos   []map[string]any
	helloAns map[string]any
	seq      int64
	badReq   int
	srv      *httptest.Server
	token    string
	host     string
}

func newFake(t *testing.T) *fakeServer {
	_, d, _ := ed25519.GenerateKey(rand.Reader)
	_, an, _ := ed25519.GenerateKey(rand.Reader)
	f := &fakeServer{t: t, dispatch: d, anchor: an, results: map[string]map[string]any{}, token: "wa_test", host: "web-prod-07",
		helloAns: map[string]any{"key_status": "confirmed", "agent_id": 17, "poll_s": 10, "halt": false}}
	f.srv = httptest.NewServer(http.HandlerFunc(f.serve))
	t.Cleanup(f.srv.Close)
	return f
}

func (f *fakeServer) checkReq(r *http.Request, body []byte) {
	sig := r.Header.Get("X-WO-Sig")
	ts := r.Header.Get("X-WO-Ts")
	_, s, _ := strings.Cut(sig, ":")
	h := sha256.Sum256(body)
	msg := "wo-req-v1\n" + r.Method + "\n" + r.URL.RequestURI() + "\n" + ts + "\n" + hex.EncodeToString(h[:])
	f.mu.Lock()
	pub := f.agentPub
	f.mu.Unlock()
	if pub != nil && !VerifyRaw([]ed25519.PublicKey{pub}, []byte(msg), s) {
		f.mu.Lock()
		f.badReq++
		f.mu.Unlock()
	}
}

func (f *fakeServer) serve(w http.ResponseWriter, r *http.Request) {
	body, _ := io.ReadAll(r.Body)
	if r.Header.Get("Authorization") != "Bearer "+f.token {
		w.WriteHeader(401)
		return
	}
	reply := func(code int, v any) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(code)
		_ = json.NewEncoder(w).Encode(v)
	}
	var env map[string]any
	if len(body) > 0 {
		_ = Decode(body, &env)
	}
	switch {
	case r.URL.Path == "/agent/v1/hello":
		b := env["body"].(map[string]any)
		pub, err := ParsePub(str(b["pubkey"]))
		if err != nil || !VerifyPurpose([]ed25519.PublicKey{pub}, "hello", b, str(env["sig"])) {
			reply(401, map[string]any{"error": "hello not signed with the reported key"})
			return
		}
		f.mu.Lock()
		f.agentPub = pub
		f.hellos = append(f.hellos, b)
		ans := map[string]any{}
		for k, v := range f.helloAns {
			ans[k] = v
		}
		f.mu.Unlock()
		f.checkReq(r, body)
		reply(200, ans)
	case r.URL.Path == "/agent/v1/jobs":
		f.checkReq(r, body)
		f.mu.Lock()
		q := f.queue
		f.queue = nil
		f.mu.Unlock()
		if len(q) == 0 {
			w.Header().Set("X-Poll-After", "10")
			w.WriteHeader(204)
			return
		}
		jobs := []any{}
		for _, e := range q {
			jobs = append(jobs, e)
		}
		f.mu.Lock()
		halt := f.helloAns["halt"]
		f.mu.Unlock()
		reply(200, map[string]any{"jobs": jobs, "cancel": []any{}, "halt": halt, "poll_s": 2})
	case strings.HasSuffix(r.URL.Path, "/start"):
		f.checkReq(r, body)
		f.mu.Lock()
		f.starts = append(f.starts, r.URL.Path)
		f.mu.Unlock()
		reply(200, map[string]any{"ok": true})
	case strings.HasSuffix(r.URL.Path, "/result"):
		f.checkReq(r, body)
		b := env["body"].(map[string]any)
		f.mu.Lock()
		ok := VerifyPurpose([]ed25519.PublicKey{f.agentPub}, "result", b, str(env["sig"]))
		b["_sig_ok"] = ok
		f.results[str(b["job_id"])] = b
		f.mu.Unlock()
		reply(200, map[string]any{"ok": true})
	default:
		reply(404, map[string]any{"error": "not found"})
	}
}

// job builds a signed envelope the way jobs._lease does (seq assigned now, signed by the dispatch key).
func (f *fakeServer) job(tmpl *Template, mode string, params map[string]any, mut func(b map[string]any)) map[string]any {
	f.mu.Lock()
	f.seq++
	seq := f.seq
	f.mu.Unlock()
	now := time.Now().UTC()
	b := map[string]any{"v": 1, "job_id": fmt.Sprintf("J-test-%04d", seq), "seq": seq, "agent_id": 17, "host": f.host, "case_id": 1,
		"run_id": 1, "step_key": "d1", "mode": mode, "template_id": tmpl.ID, "template_sha256": tmpl.SHA256, "params": params,
		"timeout_s": 60, "max_output_kb": 64, "not_before": now.Format("2006-01-02T15:04:05Z"),
		"expires_at": now.Add(5 * time.Minute).Format("2006-01-02T15:04:05Z"), "nonce": fmt.Sprintf("n%d-%d", seq, now.UnixNano()),
		"job_hash": "h", "approvals": []any{}, "audit_seq": 1}
	if mut != nil {
		mut(b)
	}
	norm, _ := normalise(b)
	sig, _ := SignPurpose(f.dispatch, "job", norm)
	return map[string]any{"body": norm, "kid": "d-test", "sig": sig}
}

func (f *fakeServer) push(envs ...map[string]any) {
	f.mu.Lock()
	f.queue = append(f.queue, envs...)
	f.mu.Unlock()
}

func (f *fakeServer) result(id string) map[string]any {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.results[id]
}

// testPack holds templates the vendor packs do not have (pid, echo with a host-listed enum).
const testPack = `{"format": 1, "pack": "test-pack", "pack_version": 1, "templates": [
 {"id": "test.proc.info", "rev": 1, "os": "linux", "kind": "diag", "risk": "R0", "argv": ["/usr/bin/jcmd", "{pid}", "VM.version"],
  "params": {"pid": {"type": "pid", "pid_exe": "java"}}, "timeout_s": 10, "max_output_kb": 8},
 {"id": "test.conf.show", "rev": 1, "os": "linux", "kind": "diag", "risk": "R0", "argv": ["/usr/bin/cat", "/etc/logrotate.d/{conf}"],
  "params": {"conf": {"type": "enum", "values_from": "host:/etc/logrotate.d"}}, "timeout_s": 10, "max_output_kb": 8}
]}`

type rig struct {
	f      *fakeServer
	a      *Actuator
	sim    string
	state  string
	action string
}

func newRig(t *testing.T, maxRisk string, policyEdit func(p map[string]any)) *rig {
	f := newFake(t)
	root := t.TempDir()
	sim := filepath.Join(root, "sandbox")
	state := filepath.Join(root, "state")
	actions := filepath.Join(sim, "etc", "watchover", "actions.d")
	scripts := filepath.Join(sim, "usr", "lib", "watchover", "actions")
	for _, d := range []string{filepath.Join(sim, "var", "log", "app"), filepath.Join(sim, ".sim"), filepath.Join(sim, "etc", "logrotate.d")} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	_ = os.WriteFile(filepath.Join(sim, "etc", "logrotate.d", "orders"), []byte("/var/log/orders/*.log {}\n"), 0o644)
	_ = os.WriteFile(filepath.Join(sim, ".sim", "disk.json"), []byte(`{"size_bytes": 1000000, "base_used_bytes": 100000}`), 0o644)
	_ = os.WriteFile(filepath.Join(sim, ".sim", "units.json"), []byte(`{"orders.service": {"ActiveState": "active", "SubState": "running", "MainPID": 4121, "NRestarts": 0}}`), 0o644)
	_, err := Install(InstallOptions{ActionsDir: actions, ScriptsDir: scripts, StateDir: state, Packs: []string{"linux-diag", "linux-act"},
		MaxRisk: maxRisk, DispatchPub: PubString(f.dispatch.Public().(ed25519.PublicKey)), AnchorPub: PubString(f.anchor.Public().(ed25519.PublicKey)),
		Sim: true})
	if err != nil {
		t.Fatal(err)
	}
	_ = os.WriteFile(filepath.Join(actions, "test-pack.tmpl.json"), []byte(testPack), 0o644)
	if policyEdit != nil {
		b, _ := os.ReadFile(filepath.Join(actions, "local.json"))
		var p map[string]any
		_ = json.Unmarshal(b, &p)
		policyEdit(p)
		b, _ = json.Marshal(p)
		_ = os.WriteFile(filepath.Join(actions, "local.json"), b, 0o644)
	}
	tok := filepath.Join(state, "actuator.token")
	_ = os.WriteFile(tok, []byte(f.token+"\n"), 0o600)
	a, err := New(Config{URL: f.srv.URL, Host: f.host, TokenFile: tok, ActionsDir: actions, StateDir: state, ScriptsDir: scripts,
		HaltFile: filepath.Join(sim, "etc", "watchover", "actions.halt"), Sim: sim, Logf: t.Logf})
	if err != nil {
		t.Fatal(err)
	}
	return &rig{f: f, a: a, sim: sim, state: state, action: actions}
}

func (r *rig) tmpl(id string) *Template {
	t := r.a.allowlist().Templates[id]
	if t == nil {
		r.f.t.Fatalf("template %s not loaded: %v", id, r.a.allowlist().Errors)
	}
	return t
}

// cycle: hello + poll + wait, like `wo-agent act --once`.
func (r *rig) cycle(t *testing.T) {
	if err := r.a.Hello(); err != nil {
		t.Fatal(err)
	}
	if r.a.active() {
		if err := r.a.Poll(); err != nil {
			t.Fatal(err)
		}
	}
	r.a.Wait()
}

func (r *rig) run(t *testing.T, envs ...map[string]any) {
	r.f.push(envs...)
	r.cycle(t)
}

func TestHelloRoundTripAndDiagnose(t *testing.T) {
	r := newRig(t, "R1", nil)
	r.cycle(t)
	h := r.f.hellos[0]
	if h["host"] != "web-prod-07" || h["sim"] != true || h["allowlist_ok"] != true {
		t.Fatalf("hello %v", h)
	}
	if pins := h["pins"].(map[string]any); pins["source"] != "install" {
		t.Fatalf("pins %v", pins)
	}
	env := r.f.job(r.tmpl("linux.disk.usage"), "diagnose", map[string]any{"path": "/var/log"}, nil)
	r.run(t, env)
	res := r.f.result("J-test-0001")
	if res == nil || res["status"] != "done" || res["_sig_ok"] != true {
		t.Fatalf("result %v", res)
	}
	fields := res["fields"].(map[string]any)
	if fields["mount"] != "/" || fields["use_pct"] == nil {
		t.Fatalf("fields %v", fields)
	}
	if r.f.badReq != 0 {
		t.Fatalf("%d requests did not carry a valid signature", r.f.badReq)
	}
	led := res["ledger"].(map[string]any)
	if str(led["n"]) != "1" || str(led["last_seq"]) != "1" {
		t.Fatalf("ledger %v", led)
	}
	// the ledger line binds the envelope exactly as the server hashes it
	if line := r.a.Ledger.LinesFrom(0, 10)[0]; line["envelope_sha256"] != SHA256Hex(env) {
		t.Fatalf("envelope sha %v", line["envelope_sha256"])
	}
}

func TestRejectCodes(t *testing.T) {
	r := newRig(t, "R1", func(p map[string]any) { p["deny"] = []string{"linux.logrotate.force"} })
	r.cycle(t)
	disk := r.tmpl("linux.disk.usage")
	_, forger, _ := ed25519.GenerateKey(rand.Reader)
	forged := r.f.job(disk, "diagnose", map[string]any{"path": "/var/log"}, nil)
	sig, _ := SignPurpose(forger, "job", forged["body"])
	forged["sig"] = sig
	_ = os.Symlink("/etc", filepath.Join(r.sim, "var", "log", "escape"))
	cases := []struct {
		env  map[string]any
		code string
	}{
		{forged, "bad_sig"},
		{r.f.job(disk, "diagnose", map[string]any{"path": "/var/log"}, func(b map[string]any) { b["host"] = "web-prod-08" }), "wrong_host"},
		{r.f.job(disk, "diagnose", map[string]any{"path": "/var/log"}, func(b map[string]any) {
			b["expires_at"] = time.Now().Add(-time.Minute).UTC().Format("2006-01-02T15:04:05Z")
		}), "expired"},
		{r.f.job(disk, "diagnose", map[string]any{"path": "/var/log"}, func(b map[string]any) { b["template_sha256"] = strings.Repeat("0", 64) }), "sha_mismatch"},
		{r.f.job(r.tmpl("linux.log.tail"), "diagnose", map[string]any{"path": "/etc/passwd"}, nil), "param_invalid"},
		{r.f.job(disk, "diagnose", map[string]any{"path": "/var/log", "extra": 1}, nil), "param_invalid"},
		{r.f.job(disk, "diagnose", map[string]any{"path": "/var/log/escape/passwd"}, nil), "path_escape"},
		{r.f.job(disk, "execute", map[string]any{"path": "/var/log"}, nil), "kind_mode"},
		{r.f.job(&Template{ID: "linux.nope", SHA256: ""}, "diagnose", map[string]any{}, nil), "no_template"},
		{r.f.job(r.tmpl("linux.service.restart"), "execute", map[string]any{"unit": "orders.service"}, nil), "risk_policy"},
		{r.f.job(r.tmpl("linux.logrotate.force"), "execute", map[string]any{"conf": "orders"}, nil), "denied_local"},
		{r.f.job(r.tmpl("test.conf.show"), "diagnose", map[string]any{"conf": "missing"}, nil), "param_invalid"},
		{r.f.job(r.tmpl("test.proc.info"), "diagnose", map[string]any{"pid": map[string]any{"pid": os.Getpid(), "start": "1:wrong"}}, nil), "pid_recycled"},
		{r.f.job(r.tmpl("linux.logs.compress_old"), "execute", map[string]any{"path": "/var/log", "days": 7}, nil), "precondition"},
	}
	for _, c := range cases {
		r.run(t, c.env)
		id := str(c.env["body"].(map[string]any)["job_id"])
		res := r.f.result(id)
		if res == nil || res["status"] != "rejected" || res["reject_code"] != c.code {
			t.Errorf("%s: want rejected %s, got %v %v", id, c.code, res["status"], res["reject_code"])
		}
	}
	if len(r.f.starts) != 0 {
		t.Fatalf("a rejected job was started: %v", r.f.starts)
	}
}

func TestSeqReplayAfterSeenLossAndDuplicate(t *testing.T) {
	r := newRig(t, "R0", nil)
	r.cycle(t)
	disk := r.tmpl("linux.disk.usage")
	first := r.f.job(disk, "diagnose", map[string]any{"path": "/var/log"}, nil)
	r.run(t, first)
	// the same envelope again: duplicate → the stored result is re-sent (no new ledger line)
	r.f.mu.Lock()
	delete(r.f.results, "J-test-0001")
	r.f.mu.Unlock()
	r.run(t, first)
	if res := r.f.result("J-test-0001"); res == nil || res["status"] != "done" {
		t.Fatalf("duplicate should resend the stored result, got %v", res)
	}
	if n := len(r.a.Ledger.LinesFrom(0, 100)); n != 1 {
		t.Fatalf("ledger lines %d", n)
	}
	// seen.json lost: a replayed seq is still refused (last_seq in its own file and in every ledger line)
	_ = os.Remove(filepath.Join(r.state, "seen.json"))
	r.a.Seen = OpenSeen(r.state)
	replay := r.f.job(disk, "diagnose", map[string]any{"path": "/var/log"}, func(b map[string]any) { b["seq"] = 1; b["job_id"] = "J-replay" })
	r.run(t, replay)
	if res := r.f.result("J-replay"); res == nil || res["reject_code"] != "seq_replay" {
		t.Fatalf("replay: %v", res)
	}
	// and after a restart (ledger re-read from disk)
	_ = os.Remove(filepath.Join(r.state, "last_seq"))
	led, _ := OpenLedger(r.state)
	if led.LastSeq() < 1 || led.Broken != "" {
		t.Fatalf("ledger reload: last_seq %d broken %q", led.LastSeq(), led.Broken)
	}
}

func TestLocalFreezeHaltAndModes(t *testing.T) {
	today := time.Now().In(time.UTC).Format("2006-01-02")
	r := newRig(t, "R2", func(p map[string]any) {
		p["freeze"] = []any{map[string]any{"from": today, "to": today, "tz": "UTC", "max_risk": "R0"}}
		p["modes"] = []string{"diagnose", "verify", "execute", "rollback"}
	})
	r.cycle(t)
	compress := r.tmpl("linux.logs.compress_old")
	e1 := r.f.job(compress, "execute", map[string]any{"path": "/var/log", "days": 7}, nil)
	e2 := r.f.job(compress, "dry_run", map[string]any{"path": "/var/log", "days": 7}, nil)
	r.run(t, e1, e2)
	if res := r.f.result("J-test-0001"); res["reject_code"] != "freeze_local" {
		t.Fatalf("freeze: %v", res["reject_code"])
	}
	if res := r.f.result("J-test-0002"); res["reject_code"] != "mode_denied" {
		t.Fatalf("mode: %v", res["reject_code"])
	}
	_ = os.WriteFile(filepath.Join(r.sim, "etc", "watchover", "actions.halt"), nil, 0o644)
	r.run(t, r.f.job(r.tmpl("linux.disk.usage"), "diagnose", map[string]any{"path": "/var/log"}, nil))
	if res := r.f.result("J-test-0003"); res["reject_code"] != "halt_local" {
		t.Fatalf("halt: %v", res["reject_code"])
	}
	if h := r.f.hellos[len(r.f.hellos)-1]; h["halt"] != true {
		t.Fatalf("hello must report the local halt")
	}
}

func TestDormantAndUnconfirmedOnlyHello(t *testing.T) {
	r := newRig(t, "R0", nil)
	r.f.helloAns["dormant"] = true
	r.f.push(r.f.job(r.tmpl("linux.disk.usage"), "diagnose", map[string]any{"path": "/var/log"}, nil))
	r.cycle(t)
	if len(r.f.results) != 0 || len(r.f.queue) != 1 {
		t.Fatalf("a dormant actuator must not poll")
	}
	r.f.helloAns["dormant"] = false
	r.f.helloAns["key_status"] = "unconfirmed"
	r.cycle(t)
	if len(r.f.queue) != 1 {
		t.Fatalf("an unconfirmed actuator must not poll")
	}
	r.f.helloAns["key_status"] = "confirmed"
	r.cycle(t)
	if len(r.f.results) != 1 {
		t.Fatalf("confirmed and awake: the job runs")
	}
}

func TestRotationAnchorAndAttestation(t *testing.T) {
	r := newRig(t, "R0", nil)
	_, newKey, _ := ed25519.GenerateKey(rand.Reader)
	newPub := PubString(newKey.Public().(ed25519.PublicKey))
	_, rogue, _ := ed25519.GenerateKey(rand.Reader)
	// a rotation signed by an unknown key is refused
	r.f.helloAns["dispatch_rotate"] = map[string]any{"new": newPub, "sig": encodeB64(ed25519.Sign(rogue, []byte("wo-rotate-v1\n"+newPub)))}
	r.cycle(t)
	if len(r.a.DispatchKeys()) != 1 {
		t.Fatalf("rogue rotation accepted")
	}
	r.f.helloAns["dispatch_rotate"] = map[string]any{"new": newPub, "sig": encodeB64(ed25519.Sign(r.f.dispatch, []byte("wo-rotate-v1\n"+newPub)))}
	anchorBody := map[string]any{"seq": 90200, "mac": "3d", "ts": "2026-10-15T10:00:00+00:00"}
	asig, _ := SignPurpose(r.f.anchor, "anchor", anchorBody)
	r.f.helloAns["anchor"] = map[string]any{"seq": 90200, "mac": "3d", "ts": "2026-10-15T10:00:00+00:00", "sig": asig}
	r.cycle(t)
	if len(r.a.DispatchKeys()) != 2 {
		t.Fatalf("signed rotation not accepted")
	}
	// jobs signed by the rotated key verify
	r.f.dispatch = newKey
	r.run(t, r.f.job(r.tmpl("linux.disk.usage"), "diagnose", map[string]any{"path": "/var/log"}, nil))
	if res := r.f.result("J-test-0001"); res["status"] != "done" {
		t.Fatalf("job under the rotated key: %v %v", res["status"], res["reject_code"])
	}
	// the next hello carries the counter-signature over the verified anchor
	r.cycle(t)
	att := r.f.hellos[len(r.f.hellos)-1]["anchor_attest"].(map[string]any)
	body := map[string]any{"seq": att["seq"], "mac": att["mac"], "ts": att["ts"], "host": "web-prod-07"}
	if att["anchor_sig_ok"] != true || !VerifyPurpose([]ed25519.PublicKey{r.f.agentPub}, "attest", body, str(att["sig"])) {
		t.Fatalf("attestation %v", att)
	}
	// a forged anchor is not attested
	r.f.helloAns["anchor"] = map[string]any{"seq": 90300, "mac": "ff", "ts": "2026-10-15T11:00:00+00:00", "sig": asig}
	r.cycle(t)
	r.cycle(t)
	att = r.f.hellos[len(r.f.hellos)-1]["anchor_attest"].(map[string]any)
	if str(att["seq"]) != "90200" {
		t.Fatalf("forged anchor attested: %v", att)
	}
}

func TestTOFUPinsWhenNoneInstalled(t *testing.T) {
	r := newRig(t, "R0", nil)
	_ = os.Remove(filepath.Join(r.action, "dispatch.pub"))
	_ = os.Remove(filepath.Join(r.action, "anchor.pub"))
	_ = os.Remove(filepath.Join(r.state, "pins.json"))
	r.run(t, r.f.job(r.tmpl("linux.disk.usage"), "diagnose", map[string]any{"path": "/var/log"}, nil))
	if res := r.f.result("J-test-0001"); res["reject_code"] != "key_unpinned" {
		t.Fatalf("without a pin nothing runs: %v", res["reject_code"])
	}
	keys := map[string]any{"dispatch": PubString(r.f.dispatch.Public().(ed25519.PublicKey)), "anchor": PubString(r.f.anchor.Public().(ed25519.PublicKey))}
	r.f.helloAns["keys"] = keys
	r.cycle(t)                                                      // the rig talks plain http: a hello answer is never pinned (security review)
	if len(r.a.DispatchKeys()) != 0 || r.a.pins()["source"] == "tofu" {
		t.Fatalf("pinned over plain http: %v", r.a.pins())
	}
	url := r.a.Cfg.URL
	r.a.Cfg.URL = "https://dashboard.example:8600"                 // over TLS the first answer is pinned
	r.a.pinFirstUse(keys)
	r.a.Cfg.URL = url
	if len(r.a.DispatchKeys()) != 1 || r.a.pins()["source"] != "tofu" {
		t.Fatalf("first-use pin: %v", r.a.pins())
	}
	// an existing pin is never replaced by a hello answer
	_, other, _ := ed25519.GenerateKey(rand.Reader)
	r.a.Cfg.URL = "https://dashboard.example:8600"
	r.a.pinFirstUse(map[string]any{"dispatch": PubString(other.Public().(ed25519.PublicKey))})
	r.a.Cfg.URL = url
	if k := r.a.DispatchKeys(); len(k) != 1 || !k[0].Equal(r.f.dispatch.Public()) {
		t.Fatalf("pin replaced")
	}
}

func TestSimulatedRestartAndUnitPredicate(t *testing.T) {
	r := newRig(t, "R2", nil)
	r.cycle(t)
	restart := r.tmpl("linux.service.restart")
	r.run(t, r.f.job(restart, "execute", map[string]any{"unit": "orders.service"}, nil),
		r.f.job(restart, "execute", map[string]any{"unit": "billing.service"}, nil))
	if res := r.f.result("J-test-0001"); res["status"] != "done" {
		t.Fatalf("restart: %v %v", res["status"], res["reject_code"])
	}
	if res := r.f.result("J-test-0002"); res["reject_code"] != "precondition" {
		t.Fatalf("missing unit: %v", res["reject_code"])
	}
	r.run(t, r.f.job(r.tmpl("linux.service.status"), "diagnose", map[string]any{"unit": "orders.service"}, nil))
	f := r.f.result("J-test-0003")["fields"].(map[string]any)
	if f["ActiveState"] != "active" || str(f["NRestarts"]) != "1" {
		t.Fatalf("status after restart %v", f)
	}
}

func TestConfigRejectCodes(t *testing.T) {
	r := newRig(t, "R2", func(p map[string]any) {
		p["hours"] = []any{map[string]any{"days": "mon-sun", "from": "00:00", "to": "00:01", "tz": "UTC"}}
	})
	r.cycle(t)
	disk := r.tmpl("linux.disk.usage")
	if r.a.allowlist().Policy.InHours(time.Now()) {
		t.Skip("the test runs in the one-minute window")
	}
	r.run(t, r.f.job(disk, "diagnose", map[string]any{"path": "/var/log"}, nil))
	if res := r.f.result("J-test-0001"); res["reject_code"] != "hours" {
		t.Fatalf("hours: %v", res["reject_code"])
	}
	future := r.f.job(disk, "diagnose", map[string]any{"path": "/var/log"}, func(b map[string]any) {
		b["not_before"] = time.Now().Add(10 * time.Minute).UTC().Format("2006-01-02T15:04:05Z")
		b["expires_at"] = time.Now().Add(20 * time.Minute).UTC().Format("2006-01-02T15:04:05Z")
	})
	r.run(t, future)
	if res := r.f.result("J-test-0002"); res["reject_code"] != "not_yet" {
		t.Fatalf("not_yet: %v", res["reject_code"])
	}
}

func TestDisabledPermsServerHaltUnconfirmedCosignAndRate(t *testing.T) {
	cases := []struct {
		name string
		edit func(p map[string]any)
		prep func(r *rig)
		tmpl string
		mode string
		args map[string]any
		code string
		n    int
	}{
		{"disabled", func(p map[string]any) { p["enabled"] = false }, nil, "linux.disk.usage", "diagnose", map[string]any{"path": "/var/log"}, "disabled", 1},
		{"perms", nil, func(r *rig) { _ = os.Chmod(filepath.Join(r.action, "linux-diag.tmpl.json"), 0o666) }, "linux.disk.usage", "diagnose",
			map[string]any{"path": "/var/log"}, "allowlist_perms", 1},
		{"halt_server", nil, func(r *rig) { r.f.helloAns["halt"] = true }, "linux.disk.usage", "diagnose", map[string]any{"path": "/var/log"}, "halt_server", 1},
		{"cosign", func(p map[string]any) {
			_, k, _ := ed25519.GenerateKey(rand.Reader)
			p["cosign"] = map[string]any{"min_risk": "R2", "keys": []string{PubString(k.Public().(ed25519.PublicKey))}}
		}, nil, "linux.service.restart", "execute", map[string]any{"unit": "orders.service"}, "cosign_missing", 1},
		{"rate", nil, nil, "linux.service.restart", "execute", map[string]any{"unit": "orders.service"}, "rate_local", 4},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			r := newRig(t, "R2", c.edit)
			r.cycle(t)
			if c.prep != nil {
				c.prep(r)
				r.cycle(t)
			}
			var last map[string]any
			for i := 0; i < c.n; i++ {
				env := r.f.job(r.tmpl(c.tmpl), c.mode, c.args, nil)
				r.run(t, env)
				last = r.f.result(str(env["body"].(map[string]any)["job_id"]))
			}
			if last == nil || last["reject_code"] != c.code {
				t.Fatalf("want %s, got %v", c.code, last)
			}
		})
	}
	// an unconfirmed key refuses even a job that reaches the actuator
	r := newRig(t, "R0", nil)
	r.f.helloAns["key_status"] = "unconfirmed"
	r.cycle(t)
	env := r.f.job(r.tmpl("linux.disk.usage"), "diagnose", map[string]any{"path": "/var/log"}, nil)
	r.a.HandleEnvelope(env)
	r.a.Wait()
	if res := r.f.result("J-test-0001"); res["reject_code"] != "key_unconfirmed" {
		t.Fatalf("key_unconfirmed: %v", res["reject_code"])
	}
}
