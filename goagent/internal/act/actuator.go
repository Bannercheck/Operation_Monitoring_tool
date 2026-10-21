package act

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/rand"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Version of the actuator role (reported in hello).
const Version = "0.8.0"

// Config of the actuator role (flags / environment of `wo-agent act`).
type Config struct {
	URL             string // dashboard receiver base, e.g. https://edge:8600 (the job channel is <URL>/agent/v1/)
	Host            string // this host's name as enrolled (jobs for any other host are refused: wrong_host)
	TokenFile       string // actuator token (wa_…), 0600
	ActionsDir      string // /etc/watchover/actions.d (root-owned allowlist and local policy)
	StateDir        string // /var/lib/watchover/actions (0700: key, ledger, seen, manifests, spool)
	ScriptsDir      string // /usr/lib/watchover/actions (sha256-pinned scripts)
	HaltFile        string // /etc/watchover/actions.halt (presence = refuse everything)
	DefaultUser     string // account for templates without run_as (dedicated, unprivileged)
	Sim             string // simulator sandbox root ("" = a real host)
	CAFile          string // extra CA bundle for https
	InsecureInstall bool   // installed over plain HTTP without a pinned checksum (reported, shown, and act modes refused server-side)
	Once            bool   // one hello / poll / run cycle, then exit (tests, `wo-agent act --once`)
	Logf            func(format string, a ...any)
	Now             func() time.Time
}

// JobRun is one accepted job.
type JobRun struct {
	ID       string
	Envelope map[string]any
	Body     map[string]any
	Tmpl     *Template
	Mode     string
	Vals     map[string]any
	Seq      int64
	Nonce    string
	Started  time.Time
	cancel   context.CancelFunc
	isAct    bool
	released func()
}

// Actuator is the running role.
type Actuator struct {
	Cfg    Config
	Env    *Env
	Units  *Units
	Key    ed25519.PrivateKey
	Ledger *Ledger
	Seen   *Seen
	Spool  *Spool
	pseudo *Pseudonymiser
	client *http.Client

	mu         sync.Mutex
	allow      *Allowlist
	keyStatus  string
	dormant    bool
	serverHalt bool
	agentID    int64
	pollS      int
	ledgerFrom int
	anchor     map[string]any
	running    map[string]*JobRun
	actSem     chan struct{}
	diagSem    chan struct{}
	lastHello  time.Time
	lastCaps   string
	wg         sync.WaitGroup
	KeyCreated bool
}

// New prepares the state directory, the agent key, the ledger and the allowlist.
func New(cfg Config) (*Actuator, error) {
	if cfg.Logf == nil {
		cfg.Logf = func(f string, a ...any) { fmt.Fprintf(os.Stderr, "watchover-actuator: "+f+"\n", a...) }
	}
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	if cfg.DefaultUser == "" {
		cfg.DefaultUser = "wo-act"
	}
	if err := os.MkdirAll(cfg.StateDir, 0o700); err != nil {
		return nil, err
	}
	key, created, err := LoadOrCreateKey(cfg.StateDir)
	if err != nil {
		return nil, err
	}
	led, err := OpenLedger(cfg.StateDir)
	if err != nil {
		return nil, err
	}
	a := &Actuator{Cfg: cfg, Env: &Env{Sim: cfg.Sim}, Key: key, Ledger: led, Seen: OpenSeen(cfg.StateDir), Spool: NewSpool(cfg.StateDir),
		pseudo: &Pseudonymiser{dir: cfg.StateDir}, keyStatus: "none", pollS: 10, ledgerFrom: -1, running: map[string]*JobRun{}, KeyCreated: created}
	a.Units = &Units{Env: a.Env}
	a.client, err = httpClient(cfg.CAFile)
	if err != nil {
		return nil, err
	}
	a.reload()
	pol := a.allow.Policy
	a.actSem = make(chan struct{}, max(1, pol.ActConcurrency))
	a.diagSem = make(chan struct{}, max(1, pol.DiagConcurrency))
	if b, err := os.ReadFile(filepath.Join(cfg.StateDir, "anchor.json")); err == nil {
		var m map[string]any
		if Decode(b, &m) == nil {
			a.anchor = m
		}
	}
	if led.Broken != "" {
		cfg.Logf("ledger: %s", led.Broken)
	}
	_ = EnsureReserve(cfg.StateDir)
	return a, nil
}

func httpClient(caFile string) (*http.Client, error) {
	tr := http.DefaultTransport.(*http.Transport).Clone()
	if caFile != "" {
		pem, err := os.ReadFile(caFile)
		if err != nil {
			return nil, err
		}
		pool, _ := x509.SystemCertPool()
		if pool == nil {
			pool = x509.NewCertPool()
		}
		if !pool.AppendCertsFromPEM(pem) {
			return nil, errors.New("no certificate in " + caFile)
		}
		tr.TLSClientConfig = &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12}
	}
	return &http.Client{Transport: tr, Timeout: 30 * time.Second}, nil
}

// Fingerprint of the agent key (what a person confirms in Bağlantı ayarları).
func (a *Actuator) Fingerprint() string { return Fingerprint(a.Key.Public().(ed25519.PublicKey)) }

// PubString of the agent key.
func (a *Actuator) PubString() string { return PubString(a.Key.Public().(ed25519.PublicKey)) }

// Pseudonym of a person's name on this host.
func (a *Actuator) Pseudonym(v string) string { return a.pseudo.Of(v) }

func (a *Actuator) reload() {
	al := LoadAllowlist(a.Cfg.ActionsDir, a.Cfg.ScriptsDir, a.Cfg.Sim != "", "linux")
	a.mu.Lock()
	a.allow = al
	a.mu.Unlock()
}

func (a *Actuator) allowlist() *Allowlist {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.allow
}

// ---------------------------------------------------------------- pinned keys

func (a *Actuator) pinPath(name string) string { return filepath.Join(a.Cfg.ActionsDir, name) }

// pinned reads actions.d/<name> (installed, root of trust) or, when absent, the first-use pin in the state directory.
func (a *Actuator) pinned(name string) []ed25519.PublicKey {
	if keys, err := ReadPubFile(a.pinPath(name)); err == nil {
		return keys
	}
	keys, _ := ReadPubFile(filepath.Join(a.Cfg.StateDir, name))
	return keys
}

// DispatchKeys: the pinned dispatch.pub plus rotations it accepted (state/dispatch.rotated).
func (a *Actuator) DispatchKeys() []ed25519.PublicKey {
	keys := a.pinned("dispatch.pub")
	if rot, err := ReadPubFile(filepath.Join(a.Cfg.StateDir, "dispatch.rotated")); err == nil {
		keys = append(keys, rot...)
	}
	return keys
}

// AnchorKeys: the pinned anchor.pub.
func (a *Actuator) AnchorKeys() []ed25519.PublicKey { return a.pinned("anchor.pub") }

func (a *Actuator) pins() map[string]any {
	out := map[string]any{"source": "none"}
	if b, err := os.ReadFile(filepath.Join(a.Cfg.StateDir, "pins.json")); err == nil {
		var m map[string]any
		if json.Unmarshal(b, &m) == nil {
			for k, v := range m {
				out[k] = v
			}
		}
	}
	if ks := a.DispatchKeys(); len(ks) > 0 {
		out["dispatch"] = Fingerprint(ks[0])
		if out["source"] == "none" {
			out["source"] = "install"
		}
	}
	if ks := a.AnchorKeys(); len(ks) > 0 {
		out["anchor"] = Fingerprint(ks[0])
	}
	return out
}

// pinFirstUse keeps dispatch.pub / anchor.pub from a hello answer when none is pinned yet (trust on first use: the
// installer pins them in actions.d from the enrolment answer when the dashboard already had keys; a dormant install gets
// them here, in the root-only state directory, so the actuator never writes into its own allowlist).
func (a *Actuator) pinFirstUse(keys map[string]any) {
	if strings.HasPrefix(strings.ToLower(a.Cfg.URL), "http://") { // over plain http the hello answer could be anyone's (security review)
		if s, _ := keys["dispatch"].(string); s != "" && len(a.pinned("dispatch.pub")) == 0 {
			a.Cfg.Logf("dispatch key not pinned over plain http: install with --dispatch-pub or use https")
		}
		return
	}
	changed := false
	for _, k := range []string{"dispatch", "anchor"} {
		s, _ := keys[k].(string)
		if s == "" {
			continue
		}
		if _, err := ParsePub(s); err != nil {
			continue
		}
		if len(a.pinned(k+".pub")) > 0 {
			continue
		}
		p := filepath.Join(a.Cfg.StateDir, k+".pub")
		if err := writeFileDurable(p, []byte(s+"\n")); err != nil {
			a.Cfg.Logf("cannot pin %s.pub: %v", k, err)
			continue
		}
		changed = true
		a.Cfg.Logf("%s key pinned on first use: %s", k, func() string { pk, _ := ParsePub(s); return Fingerprint(pk) }())
	}
	if changed {
		_ = writeFileDurable(filepath.Join(a.Cfg.StateDir, "pins.json"), MustCanonical(map[string]any{"source": "tofu", "at": a.now().UTC().Format(time.RFC3339)}))
	}
}

// acceptRotation: a new dispatch key signed by a key already trusted, over "wo-rotate-v1\n" + new (§9.3.1). The pinned file
// stays the root of trust; accepted keys go to state/dispatch.rotated.
func (a *Actuator) acceptRotation(rot map[string]any) bool {
	newKey, _ := rot["new"].(string)
	sig, _ := rot["sig"].(string)
	pk, err := ParsePub(newKey)
	if err != nil {
		return false
	}
	trusted := a.DispatchKeys()
	for _, k := range trusted {
		if bytes.Equal(k, pk) {
			return true
		}
	}
	if !VerifyRaw(trusted, []byte("wo-rotate-v1\n"+newKey), sig) {
		a.Cfg.Logf("dispatch key rotation refused: not signed by a trusted dispatch key")
		return false
	}
	f, err := os.OpenFile(filepath.Join(a.Cfg.StateDir, "dispatch.rotated"), os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o600)
	if err != nil {
		return false
	}
	defer f.Close()
	_, err = f.WriteString(newKey + "\n")
	if err == nil {
		err = f.Sync()
	}
	a.Cfg.Logf("dispatch key rotation accepted: %s", Fingerprint(pk))
	return err == nil
}

// verifyAnchor checks an anchor with the pinned anchor.pub and keeps it for the counter-signature (§10.7, R3b).
func (a *Actuator) verifyAnchor(an map[string]any) bool {
	body := map[string]any{"seq": an["seq"], "mac": an["mac"], "ts": an["ts"]}
	sig, _ := an["sig"].(string)
	if !VerifyPurpose(a.AnchorKeys(), "anchor", body, sig) {
		return false
	}
	a.mu.Lock()
	a.anchor = body
	a.mu.Unlock()
	_ = writeFileDurable(filepath.Join(a.Cfg.StateDir, "anchor.json"), MustCanonical(body))
	return true
}

// attest is the counter-signature over the last verified anchor (purpose attest, agent key).
func (a *Actuator) attest() map[string]any {
	a.mu.Lock()
	an := a.anchor
	a.mu.Unlock()
	if an == nil {
		return nil
	}
	body := map[string]any{"seq": an["seq"], "mac": an["mac"], "ts": an["ts"], "host": a.Cfg.Host}
	sig, err := SignPurpose(a.Key, "attest", body)
	if err != nil {
		return nil
	}
	return map[string]any{"seq": an["seq"], "mac": an["mac"], "ts": an["ts"], "anchor_sig_ok": true, "sig": sig}
}

// ---------------------------------------------------------------- transport

func (a *Actuator) token() string {
	b, err := os.ReadFile(a.Cfg.TokenFile)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(b))
}

func (a *Actuator) now() time.Time { return a.Cfg.Now() }

// request sends a signed /agent/v1/ request (X-WO-Sig over "wo-req-v1\n" + method + "\n" + path + "\n" + ts + "\n" + sha256(body)).
func (a *Actuator) request(method, path string, body any) (int, http.Header, map[string]any, error) {
	var raw []byte
	if body != nil {
		var buf bytes.Buffer
		enc := json.NewEncoder(&buf)
		enc.SetEscapeHTML(false)
		if err := enc.Encode(body); err != nil {
			return 0, nil, nil, err
		}
		raw = bytes.TrimRight(buf.Bytes(), "\n")
	}
	req, err := http.NewRequest(method, strings.TrimRight(a.Cfg.URL, "/")+path, bytes.NewReader(raw))
	if err != nil {
		return 0, nil, nil, err
	}
	ts := strconv.FormatInt(a.now().Unix(), 10)
	h := sha256.Sum256(raw)
	msg := "wo-req-v1\n" + method + "\n" + path + "\n" + ts + "\n" + hex.EncodeToString(h[:])
	req.Header.Set("Authorization", "Bearer "+a.token())
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-WO-Ts", ts)
	req.Header.Set("X-WO-Sig", "agent:"+base64.StdEncoding.EncodeToString(ed25519.Sign(a.Key, []byte(msg))))
	req.Header.Set("User-Agent", "wo-agent/"+Version)
	resp, err := a.client.Do(req)
	if err != nil {
		return 0, nil, nil, err
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	var out map[string]any
	if len(bytes.TrimSpace(b)) > 0 {
		_ = Decode(b, &out)
	}
	return resp.StatusCode, resp.Header, out, nil
}

// ---------------------------------------------------------------- hello

func (a *Actuator) haltLocal() bool {
	_, err := os.Stat(a.Cfg.HaltFile)
	return err == nil
}

func (a *Actuator) templateList(al *Allowlist) []any {
	ids := make([]string, 0, len(al.Templates))
	for id := range al.Templates {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	out := []any{}
	for _, id := range ids {
		t := al.Templates[id]
		out = append(out, map[string]any{"id": t.ID, "rev": t.Rev, "sha256": t.SHA256, "risk": t.Risk, "kind": t.Kind, "os": t.OS})
	}
	return out
}

// HelloBody is what the actuator reports (§9.3.2).
func (a *Actuator) HelloBody() map[string]any {
	al := a.allowlist()
	tl := a.templateList(al)
	pol := map[string]any{"enabled": al.Policy.Enabled, "max_risk": al.Policy.MaxRisk, "deny": anyOr(al.Policy.Deny, []string{}),
		"cosign_keys": al.CosignKeys, "modes": al.Policy.Modes}
	running := []any{}
	a.mu.Lock()
	for id := range a.running {
		running = append(running, id)
	}
	from := a.ledgerFrom
	a.mu.Unlock()
	kids := []any{}
	for _, k := range a.DispatchKeys() {
		kids = append(kids, Kid("d-", k))
	}
	b := map[string]any{"v": 1, "host": a.Cfg.Host, "actuator": Version, "os": "linux", "arch": runtime.GOARCH, "pubkey": a.PubString(),
		"clock": a.now().UTC().Format("2006-01-02T15:04:05Z"), "templates": tl, "caps_sha256": SHA256Hex(map[string]any{"templates": tl, "policy": pol}),
		"policy": pol, "policy_sha256": al.PolicySHA, "freeze_sha256": al.FreezeSHA, "insecure_install": a.Cfg.InsecureInstall,
		"allowlist_ok": al.PermsOK, "halt": a.haltLocal(), "ledger": a.Ledger.State(), "running": running, "dispatch_kids": kids,
		"pins": a.pins(), "sim": a.Cfg.Sim != ""}
	if !al.PermsOK {
		b["allowlist_detail"] = al.PermsDetail
	}
	if len(al.Errors) > 0 {
		errs := al.Errors
		if len(errs) > 10 {
			errs = errs[:10]
		}
		b["allowlist_errors"] = errs
	}
	if at := a.attest(); at != nil {
		b["anchor_attest"] = at
	}
	if from >= 0 {
		lines := []any{}
		for _, ln := range a.Ledger.LinesFrom(from, 500) {
			lines = append(lines, ln)
		}
		b["ledger_lines"] = lines
	}
	return b
}

// Hello registers / heartbeats and applies the answer (key status, dormancy, halt, anchors, rotations, ledger requests).
func (a *Actuator) Hello() error {
	a.reload()
	b := a.HelloBody()
	sig, err := SignPurpose(a.Key, "hello", b)
	if err != nil {
		return err
	}
	code, _, ans, err := a.request("POST", "/agent/v1/hello", map[string]any{"body": b, "kid": "agent", "sig": sig})
	if err != nil {
		return err
	}
	a.mu.Lock()
	a.lastHello = a.now()
	a.lastCaps, _ = b["caps_sha256"].(string)
	a.mu.Unlock()
	if code != 200 {
		msg, _ := ans["error"].(string)
		return fmt.Errorf("hello refused (HTTP %d): %s", code, msg)
	}
	a.mu.Lock()
	if _, sent := b["ledger_lines"]; sent {
		a.ledgerFrom = -1
	}
	if s, ok := ans["key_status"].(string); ok {
		a.keyStatus = s
	}
	a.dormant, _ = ans["dormant"].(bool)
	a.serverHalt, _ = ans["halt"].(bool)
	if n, ok := numOpt(ans, "agent_id"); ok {
		a.agentID = n
	}
	if n, ok := numOpt(ans, "poll_s"); ok && n >= 2 && n <= 300 {
		a.pollS = int(n)
	}
	if n, ok := numOpt(ans, "ledger_from"); ok {
		a.ledgerFrom = int(n)
	}
	a.mu.Unlock()
	if keys, ok := ans["keys"].(map[string]any); ok {
		a.pinFirstUse(keys)
	}
	if rot, ok := ans["dispatch_rotate"].(map[string]any); ok {
		a.acceptRotation(rot)
	}
	if an, ok := ans["anchor"].(map[string]any); ok {
		if !a.verifyAnchor(an) {
			a.Cfg.Logf("anchor %v does not verify with the pinned anchor.pub", an["seq"])
		}
	}
	if m, _ := ans["ledger_mismatch"].(bool); m {
		a.Cfg.Logf("the dashboard reports a ledger mismatch: the host is stopped until a person checks it")
	}
	return nil
}

// Status for the CLI and tests.
func (a *Actuator) Status() map[string]any {
	a.mu.Lock()
	defer a.mu.Unlock()
	return map[string]any{"key_status": a.keyStatus, "dormant": a.dormant, "halt": a.serverHalt, "agent_id": a.agentID}
}

func (a *Actuator) active() bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.keyStatus == "confirmed" && !a.dormant
}

// ---------------------------------------------------------------- poll

// Poll asks for jobs once and starts every accepted one.
func (a *Actuator) Poll() error {
	code, hdr, ans, err := a.request("GET", "/agent/v1/jobs?after="+strconv.FormatInt(a.Ledger.LastSeq(), 10), nil)
	if err != nil {
		return err
	}
	switch code {
	case 204:
		if n, err := strconv.Atoi(hdr.Get("X-Poll-After")); err == nil && n >= 2 && n <= 300 {
			a.mu.Lock()
			a.pollS = n
			a.mu.Unlock()
		}
		return nil
	case 200:
	case 403:
		a.mu.Lock()
		a.keyStatus = "unconfirmed"
		a.mu.Unlock()
		return errors.New("key not confirmed")
	default:
		msg, _ := ans["error"].(string)
		return fmt.Errorf("poll refused (HTTP %d): %s", code, msg)
	}
	a.mu.Lock()
	a.serverHalt, _ = ans["halt"].(bool)
	if n, ok := numOpt(ans, "poll_s"); ok && n >= 2 && n <= 300 {
		a.pollS = int(n)
	}
	a.mu.Unlock()
	for _, c := range strList(ans["cancel"]) {
		a.mu.Lock()
		jr := a.running[c]
		a.mu.Unlock()
		if jr != nil && jr.cancel != nil {
			if v, ok := jr.Tmpl.Raw["abort_on_kill"].(bool); !ok || v {
				jr.cancel()
			}
		}
	}
	jobs, _ := ans["jobs"].([]any)
	for _, j := range jobs {
		if env, ok := j.(map[string]any); ok {
			a.HandleEnvelope(env)
		}
	}
	return nil
}

// ---------------------------------------------------------------- checks (§9.3.4, in order; any failure → rejected)

func modeKindOK(kind, mode string) bool {
	if kind == "diag" {
		return mode == "diagnose" || mode == "verify"
	}
	return mode == "dry_run" || mode == "execute" || mode == "rollback"
}

func isActMode(mode string) bool { return mode == "execute" || mode == "rollback" }

func parseTime(s string) (time.Time, bool) {
	t, err := time.Parse(time.RFC3339, s)
	return t, err == nil
}

// check runs §9.3.4 #1–#9; "" or the reject code. It records the seq / job id once #2–#4 pass (R6).
func (a *Actuator) check(jr *JobRun) string {
	al := a.allowlist()
	pol := &al.Policy
	b := jr.Body
	// 1 configuration
	a.mu.Lock()
	keyStatus, serverHalt, agentID := a.keyStatus, a.serverHalt, a.agentID
	a.mu.Unlock()
	switch {
	case !pol.Enabled:
		return "disabled"
	case !al.PermsOK:
		return "allowlist_perms"
	case a.haltLocal():
		return "halt_local"
	case serverHalt:
		return "halt_server"
	case pol.RequireConfirmedKey() && keyStatus != "confirmed":
		return "key_unconfirmed"
	}
	// 2 signature with a pinned or rotated dispatch key
	keys := a.DispatchKeys()
	if len(keys) == 0 {
		return "key_unpinned"
	}
	sig, _ := jr.Envelope["sig"].(string)
	if !VerifyPurpose(keys, "job", b, sig) {
		return "bad_sig"
	}
	// 3 host, agent, time window
	if h, _ := b["host"].(string); h != a.Cfg.Host {
		return "wrong_host"
	}
	if id, ok := numOpt(b, "agent_id"); ok && agentID != 0 && id != agentID {
		return "wrong_host"
	}
	now := a.now()
	nb, ok1 := parseTime(str(b["not_before"]))
	ex, ok2 := parseTime(str(b["expires_at"]))
	if !ok1 || !ok2 {
		return "expired"
	}
	if now.Before(nb.Add(-120 * time.Second)) {
		return "not_yet"
	}
	if now.After(ex) {
		return "expired"
	}
	// 4 duplicate / replay
	if a.Seen.Has(jr.ID, jr.Nonce) {
		return "duplicate"
	}
	if jr.Seq <= a.Ledger.LastSeq() {
		return "seq_replay"
	}
	if err := a.Ledger.SetLastSeq(jr.Seq); err != nil {
		return "state_error"
	}
	_ = a.Seen.Add(jr.ID, jr.Nonce, now)
	// 5 local policy: mode, hours, freeze
	if !pol.ModeAllowed(jr.Mode) {
		return "mode_denied"
	}
	if !pol.InHours(now) {
		return "hours"
	}
	// 6 template
	t := al.Templates[str(b["template_id"])]
	if t == nil {
		return "no_template"
	}
	jr.Tmpl = t
	if want := str(b["template_sha256"]); want != "" && want != t.SHA256 {
		return "sha_mismatch"
	}
	if t.OS != "linux" {
		return "os_mismatch"
	}
	if !modeKindOK(t.Kind, jr.Mode) || (jr.Mode == "dry_run" && !t.Bool("dry_run")) {
		return "kind_mode"
	}
	if fr := pol.FreezeMaxRisk(now); fr != "" && riskRankOf(t.Risk) > riskRankOf(fr) {
		return "freeze_local"
	}
	if riskRankOf(t.Risk) > riskRankOf(pol.MaxRisk) {
		return "risk_policy"
	}
	if pol.Denied(t.ID) {
		return "denied_local"
	}
	if al.CosignKeys > 0 {
		minRisk := "R2"
		if pol.Cosign != nil {
			if m := str(pol.Cosign["min_risk"]); m != "" {
				minRisk = m
			}
		}
		if riskRankOf(t.Risk) >= riskRankOf(minRisk) && !a.cosigned(jr, pol) {
			return "cosign_missing"
		}
	}
	if sc, ok := t.Raw["script"].(map[string]any); ok {
		data, err := os.ReadFile(a.Env.Real(str(sc["path"])))
		h := sha256.Sum256(data)
		if err != nil || hex.EncodeToString(h[:]) != str(sc["sha256"]) {
			return "sha_mismatch"
		}
	}
	// 7 parameters (local validators incl. realpath)
	params, _ := b["params"].(map[string]any)
	ctx := &ParamCtx{Host: a.Cfg.Host, AllowHosts: pol.AllowHosts, Real: a.Env.Real, Logical: a.Env.Logical}
	vals, err := ValidateParams(t, params, ctx)
	if err != nil {
		if ErrCode(err) == "symlink_escape" {
			return "path_escape"
		}
		a.Cfg.Logf("job %s: parameter refused: %v", jr.ID, err)
		return "param_invalid"
	}
	for name, spec := range t.Params {
		if vf := str(spec["values_from"]); strings.HasPrefix(vf, "host:") && !a.hostValue(vf[5:], str(vals[name])) {
			return "param_invalid"
		}
	}
	jr.Vals = vals
	// 8 predicates and host checks
	pe := &predEnv{env: a.Env, units: a.Units, host: a.Cfg.Host, vals: vals, now: now}
	for _, p := range preList(t) {
		if ok, why := pe.eval(p.name, p.args); !ok {
			a.Cfg.Logf("job %s: precondition %s failed: %s", jr.ID, p.name, why)
			return "precondition"
		}
	}
	for name, spec := range t.Params {
		if str(spec["type"]) != "pid" {
			continue
		}
		pid, start := pidOf(vals[name])
		pi, ok := ReadProc(pid)
		exe := str(spec["pid_exe"])
		if exe == "" {
			exe = t.Str("pid_exe")
		}
		if !ok || pi.Start != start || (exe != "" && pi.Comm != exe && filepath.Base(pi.Exe) != exe) {
			return "pid_recycled"
		}
	}
	// 9 local limits
	hour := now.Add(-time.Hour)
	if lim, ok := t.Raw["limits"].(map[string]any); ok {
		if ph, ok := numOpt(lim, "per_hour"); ok && int64(a.Ledger.CountSince(hour, func(m map[string]any) bool { return m["template_id"] == t.ID })) >= ph {
			return "rate_local"
		}
	}
	isAct := t.Kind == "act"
	perHour := pol.MaxDiagPerHour
	if isAct {
		perHour = pol.MaxActPerHour
	}
	if perHour > 0 && a.Ledger.CountSince(hour, func(m map[string]any) bool { return isActMode(str(m["mode"])) == isActMode(jr.Mode) }) >= perHour {
		return "rate_local"
	}
	sem := a.diagSem
	if isAct {
		sem = a.actSem
	}
	select {
	case sem <- struct{}{}:
		jr.isAct = isAct
		jr.released = func() { <-sem }
	default:
		return "busy"
	}
	return ""
}

// hostValue: an enum whose values come from the host (values_from "host:<dir>") must name a file there.
func (a *Actuator) hostValue(dir, v string) bool {
	if v == "" || strings.ContainsAny(v, "/\\") {
		return false
	}
	fi, err := os.Stat(filepath.Join(a.Env.Real(dir), v))
	return err == nil && fi.Mode().IsRegular()
}

// cosigned: a co-signature (WP20) by a key listed in local.json cosign.keys over "wo-cosign-v1\n" + job_hash.
func (a *Actuator) cosigned(jr *JobRun, pol *Policy) bool {
	co, ok := jr.Envelope["cosig"].(map[string]any)
	if !ok {
		return false
	}
	var keys []ed25519.PublicKey
	for _, k := range strList(pol.Cosign["keys"]) {
		if pk, err := ParsePub(k); err == nil {
			keys = append(keys, pk)
		}
	}
	return VerifyRaw(keys, []byte("wo-cosign-v1\n"+str(jr.Body["job_hash"])), str(co["sig"]))
}

// ---------------------------------------------------------------- jobs

// HandleEnvelope checks one envelope; a rejected job is answered at once, an accepted one is started and runs in the
// background (one act job and two diagnose jobs at a time).
func (a *Actuator) HandleEnvelope(env map[string]any) {
	body, _ := env["body"].(map[string]any)
	if body == nil {
		return
	}
	jr := &JobRun{ID: str(body["job_id"]), Envelope: env, Body: body, Mode: str(body["mode"]), Nonce: str(body["nonce"]), Started: a.now()}
	if s, ok := numOpt(body, "seq"); ok {
		jr.Seq = s
	}
	if jr.ID == "" {
		return
	}
	if code := a.check(jr); code != "" {
		if code == "duplicate" {
			if stored, _, ok := a.Spool.Get(jr.ID); ok {
				a.postResult(jr.ID, stored)
				return
			}
		}
		a.Cfg.Logf("job %s (%s %s) rejected: %s", jr.ID, str(body["template_id"]), jr.Mode, code)
		a.finish(jr, Outcome{Status: "rejected", Reject: code})
		return
	}
	code, _, ans, err := a.request("POST", "/agent/v1/jobs/"+jr.ID+"/start", map[string]any{})
	if err != nil || code != 200 {
		jr.released()
		a.Cfg.Logf("job %s: start not acknowledged (%v, HTTP %d); not run", jr.ID, err, code)
		a.finish(jr, Outcome{Status: "cancelled", Reject: "start_failed"})
		return
	}
	if ok, _ := ans["ok"].(bool); !ok {
		jr.released()
		a.finish(jr, Outcome{Status: "cancelled"})
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(jr.Tmpl.Int("timeout_s", 120))*time.Second)
	jr.cancel = cancel
	a.mu.Lock()
	a.running[jr.ID] = jr
	a.mu.Unlock()
	a.wg.Add(1)
	go func() {
		defer a.wg.Done()
		defer cancel()
		out := a.execute(ctx, jr)
		jr.released()
		a.mu.Lock()
		delete(a.running, jr.ID)
		a.mu.Unlock()
		a.finish(jr, out)
	}()
}

// Outcome of one job as reported.
type Outcome struct {
	Status    string // done | failed | timed_out | cancelled | rejected
	Reject    string
	ExitCode  *int
	Fields    map[string]any
	Display   string
	Stderr    string
	Truncated bool
	OutputSHA string
	Privacy   string
}

func intp(n int) *int { return &n }

func (a *Actuator) baseEnv(t *Template) []string {
	env := []string{"PATH=/usr/sbin:/usr/bin:/sbin:/bin", "LANG=C"}
	if m, ok := t.Raw["env"].(map[string]any); ok {
		keys := make([]string, 0, len(m))
		for k := range m {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			env = append(env, k+"="+str(m[k]))
		}
	}
	return env
}

// runAs resolves the template's run_as ("" → the dedicated default user; {pid_owner}; {sid_lower}adm).
func (a *Actuator) runAs(t *Template, vals map[string]any) (*RunAs, error) {
	if a.Cfg.Sim != "" {
		return nil, nil
	}
	spec := t.Str("run_as")
	name := spec
	switch {
	case spec == "":
		name = a.Cfg.DefaultUser
		if _, err := LookupRunAs(name); err != nil {
			name = "nobody"
		}
	case strings.Contains(spec, "{"):
		for _, ph := range placeholders(spec) {
			base, how, _ := strings.Cut(ph, "_")
			v := vals[base]
			switch how {
			case "lower":
				name = strings.ReplaceAll(name, "{"+ph+"}", strings.ToLower(valueString(v)))
			case "owner":
				pid, _ := pidOf(v)
				pi, ok := ReadProc(pid)
				if !ok {
					return nil, errors.New("process gone")
				}
				name = strings.ReplaceAll(name, "{"+ph+"}", UserName(pi.UID))
			default:
				return nil, fmt.Errorf("run_as placeholder %s", ph)
			}
		}
	}
	return LookupRunAs(name)
}

func tail(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[len(s)-n:]
}

// execute runs an accepted job and builds its outcome (fields scrubbed by type, raw text scrubbed for display).
func (a *Actuator) execute(ctx context.Context, jr *JobRun) Outcome {
	t := jr.Tmpl
	out := Outcome{Privacy: t.Privacy()}
	parser := t.Parser()
	var stdout string
	if name := t.Str("builtin"); name != "" {
		fn := builtins[name]
		if fn == nil {
			return Outcome{Status: "failed", Reject: "", Stderr: "built-in not available on this host", ExitCode: intp(127)}
		}
		type res struct {
			o   BuiltinOut
			err error
		}
		ch := make(chan res, 1)
		go func() {
			o, err := fn(&BuiltinCall{Ctx: ctx, A: a, Job: jr, Vals: jr.Vals, Dry: jr.Mode == "dry_run", Tmpl: t, Start: jr.Started})
			ch <- res{o, err}
		}()
		var r res
		select {
		case r = <-ch:
		case <-ctx.Done():
			r = <-ch // built-ins watch the context; wait for them to stop touching files
		}
		if r.o.Reject != "" {
			return Outcome{Status: "rejected", Reject: r.o.Reject}
		}
		out.Fields, out.Display = r.o.Fields, r.o.Display
		switch {
		case errors.Is(ctx.Err(), context.DeadlineExceeded):
			out.Status, out.ExitCode, out.Stderr = "timed_out", intp(124), "timeout"
		case errors.Is(ctx.Err(), context.Canceled):
			out.Status, out.ExitCode = "cancelled", intp(130)
		case r.err != nil:
			out.Status, out.ExitCode, out.Stderr = "failed", intp(1), r.err.Error()
		default:
			out.Status, out.ExitCode = "done", intp(0)
		}
		stdout = string(MustCanonical(anyOr(out.Fields, map[string]any{}))) + out.Display
	} else {
		argv, err := a.render(t, jr)
		if err != nil {
			return Outcome{Status: "rejected", Reject: "param_invalid"}
		}
		var er ExecResult
		if a.Cfg.Sim != "" {
			er = a.simulate(argv)
		} else {
			as, err := a.runAs(t, jr.Vals)
			if err != nil {
				return Outcome{Status: "rejected", Reject: "precondition", Stderr: err.Error()}
			}
			er, err = RunArgv(ctx, argv, as, a.baseEnv(t), t.Int("max_output_kb", 64)*1024, time.Duration(t.Int("timeout_s", 120))*time.Second)
			if err != nil && er.ExitCode == 0 {
				er.ExitCode = 127
				er.Stderr = []byte(err.Error())
			}
		}
		stdout = string(er.Stdout)
		out.ExitCode, out.Truncated, out.Stderr = intp(er.ExitCode), er.Truncated, string(er.Stderr)
		switch {
		case er.TimedOut:
			out.Status = "timed_out"
		case er.Cancelled:
			out.Status = "cancelled"
		case er.ExitCode == 0:
			out.Status = "done"
		default:
			out.Status = "failed"
		}
		if out.Privacy == "structured" {
			out.Fields = ParseOutput(parser, stdout)
		}
		if out.Fields == nil {
			out.Display = stdout
		}
	}
	h := sha256.Sum256([]byte(stdout))
	out.OutputSHA = hex.EncodeToString(h[:])
	if out.Fields != nil {
		if f, ok := ScrubFields(out.Fields, SchemaFor(parser), a.pseudo).(map[string]any); ok {
			out.Fields = f
		}
	}
	out.Display = ScrubStrict(tail(out.Display, 64*1024))
	out.Stderr = ScrubStrict(tail(out.Stderr, 4096))
	return out
}

// render builds the argv the template declares (dry_argv for dry runs, the pinned script with its arguments).
func (a *Actuator) render(t *Template, jr *JobRun) ([]string, error) {
	var src []string
	if sc, ok := t.Raw["script"].(map[string]any); ok {
		src = append([]string{str(sc["path"])}, strList(sc["args"])...)
	} else if jr.Mode == "dry_run" && len(t.Strings("dry_argv")) > 0 {
		src = t.Strings("dry_argv")
	} else {
		src = t.Strings("argv")
	}
	out := make([]string, 0, len(src))
	for _, el := range src {
		s, err := Fill(el, jr.Vals)
		if err != nil {
			return nil, err
		}
		out = append(out, s)
	}
	if len(out) > 0 && a.Cfg.Sim == "" {
		if _, err := os.Stat(out[0]); err != nil {
			return nil, err
		}
	}
	return out, nil
}

// finish writes the ledger line, signs the result, spools it and posts it.
func (a *Actuator) finish(jr *JobRun, o Outcome) {
	end := a.now()
	if o.Fields == nil {
		o.Fields = map[string]any{}
	}
	if o.Privacy == "" {
		o.Privacy = "structured"
		if jr.Tmpl != nil {
			o.Privacy = jr.Tmpl.Privacy()
		}
	}
	a.mu.Lock()
	an := a.anchor
	a.mu.Unlock()
	line := map[string]any{"ts": end.UTC().Format("2006-01-02T15:04:05Z"), "started": jr.Started.UTC().Format(time.RFC3339Nano), "job_id": jr.ID,
		"nonce": jr.Nonce, "seq": jr.Seq, "envelope_sha256": SHA256Hex(jr.Envelope), "template_id": str(jr.Body["template_id"]),
		"template_sha256": str(jr.Body["template_sha256"]), "params_sha256": SHA256Hex(anyOr(jr.Body["params"], map[string]any{})), "mode": jr.Mode,
		"status": o.Status, "reject_code": o.Reject, "exit_code": o.ExitCode, "output_sha256": o.OutputSHA, "anchor_seq": nil, "anchor_mac": ""}
	if an != nil {
		line["anchor_seq"], line["anchor_mac"] = an["seq"], an["mac"]
	}
	if _, err := a.Ledger.Append(line); err != nil {
		a.Cfg.Logf("ledger write failed: %v", err)
	}
	a.Cfg.Logf("job %s %s %s: %s %s", jr.ID, str(jr.Body["template_id"]), jr.Mode, o.Status, o.Reject)
	rb := map[string]any{"v": 1, "job_id": jr.ID, "nonce": jr.Nonce, "seq": jr.Seq, "host": a.Cfg.Host, "status": o.Status, "reject_code": o.Reject,
		"exit_code": o.ExitCode, "started": jr.Started.UTC().Format("2006-01-02T15:04:05Z"), "ended": end.UTC().Format("2006-01-02T15:04:05Z"),
		"template_sha256": str(jr.Body["template_sha256"]), "privacy": o.Privacy, "fields": o.Fields, "display_tail": o.Display,
		"stderr_tail": o.Stderr, "truncated": o.Truncated, "output_sha256": o.OutputSHA, "ledger": a.Ledger.State()}
	norm, err := normalise(rb)
	if err != nil {
		a.Cfg.Logf("result of %s cannot be encoded: %v", jr.ID, err)
		return
	}
	sig, err := SignPurpose(a.Key, "result", norm)
	if err != nil {
		a.Cfg.Logf("result of %s cannot be signed: %v", jr.ID, err)
		return
	}
	env := map[string]any{"body": norm, "kid": "agent", "sig": sig}
	if err := a.Spool.Put(jr.ID, env); err != nil {
		a.Cfg.Logf("result spool write failed: %v", err)
	}
	a.postResult(jr.ID, env)
}

func (a *Actuator) postResult(jobID string, env map[string]any) bool {
	code, _, ans, err := a.request("POST", "/agent/v1/jobs/"+jobID+"/result", env)
	if err != nil {
		return false
	}
	if code == 200 || code == 404 || code == 409 { // acknowledged, or a job the dashboard will never take a result for
		if code != 200 {
			a.Cfg.Logf("result of %s dropped by the dashboard (HTTP %d): %v", jobID, code, ans["error"])
		}
		a.Spool.Ack(jobID)
		return true
	}
	return false
}

// FlushSpool re-sends results that were not acknowledged.
func (a *Actuator) FlushSpool() {
	for _, id := range a.Spool.Pending(a.now()) {
		if env, _, ok := a.Spool.Get(id); ok {
			a.postResult(id, env)
		}
	}
}

// Wait for running jobs (tests and --once).
func (a *Actuator) Wait() { a.wg.Wait() }

// ---------------------------------------------------------------- the loop

func jitter(s int) time.Duration {
	return time.Duration(s)*time.Second + time.Duration(rand.Intn(4001)-2000)*time.Millisecond
}

// Run is the actuator loop: hello at start, every 5 minutes and whenever the capabilities change; while act mode is off
// on the dashboard (dormant) or the key is not confirmed yet, only hello; otherwise poll every 10 s ± 2 s (2 s while a job
// runs), run jobs, re-send unacknowledged results.
func (a *Actuator) Run(ctx context.Context) error {
	if a.Cfg.URL == "" || a.Cfg.Host == "" {
		return errors.New("--url and --host are required")
	}
	a.Cfg.Logf("actuator %s for %s, key %s%s", Version, a.Cfg.Host, a.Fingerprint(), map[bool]string{true: " (simulator: " + a.Cfg.Sim + ")", false: ""}[a.Cfg.Sim != ""])
	for {
		if a.token() == "" {
			a.Cfg.Logf("no actuator token in %s: waiting (Ajanlar panelinden eylem ajanı jetonu)", a.Cfg.TokenFile)
		} else {
			a.reload()
			a.mu.Lock()
			since := a.now().Sub(a.lastHello)
			unconf := a.keyStatus != "confirmed"
			caps := a.lastCaps
			from := a.ledgerFrom
			a.mu.Unlock()
			cur, _ := a.HelloBody()["caps_sha256"].(string)
			if a.lastHello.IsZero() || since >= 5*time.Minute || cur != caps || from >= 0 || (unconf && since >= time.Minute) {
				if err := a.Hello(); err != nil {
					a.Cfg.Logf("%v", err)
				}
			}
			if a.active() {
				if err := a.Poll(); err != nil {
					a.Cfg.Logf("%v", err)
				}
			}
			a.FlushSpool()
		}
		if a.Cfg.Once {
			a.Wait()
			a.FlushSpool()
			return nil
		}
		wait := time.Minute
		a.mu.Lock()
		switch {
		case len(a.running) > 0:
			wait = 2 * time.Second
		case a.keyStatus == "confirmed" && !a.dormant:
			wait = jitter(a.pollS)
		case a.dormant:
			wait = 5 * time.Minute
		}
		a.mu.Unlock()
		select {
		case <-ctx.Done():
			a.Wait()
			return nil
		case <-time.After(wait):
		}
	}
}
