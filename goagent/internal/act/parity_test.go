package act

import (
	"bufio"
	"bytes"
	"crypto/ed25519"
	"encoding/hex"
	"os"
	"path/filepath"
	"testing"
)

// Shared fixtures with the Python server (tests/act_parity.py writes them; tests/test_act_goagent.py keeps them current).
// In the receiver image they are copied next to goagent/ so the same relative path works.
func fixture(t *testing.T, name string) string {
	p := filepath.Join("..", "..", "..", "tests", "fixtures", name)
	if _, err := os.Stat(p); err != nil {
		t.Skipf("fixture %s not present (%v)", name, err)
	}
	return p
}

func jsonl(t *testing.T, name string) []map[string]any {
	f, err := os.Open(fixture(t, name))
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	var out []map[string]any
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	for sc.Scan() {
		if len(bytes.TrimSpace(sc.Bytes())) == 0 {
			continue
		}
		var m map[string]any
		if err := Decode(sc.Bytes(), &m); err != nil {
			t.Fatal(err)
		}
		out = append(out, m)
	}
	return out
}

func TestCanonicalParity(t *testing.T) {
	rows := jsonl(t, "act_canonical.jsonl")
	if len(rows) < 20 {
		t.Fatalf("only %d rows", len(rows))
	}
	for _, r := range rows {
		switch r["kind"] {
		case "object":
			got, err := Canonical(r["in"])
			if err != nil {
				t.Fatal(err)
			}
			if string(got) != r["canonical"] {
				t.Errorf("canonical mismatch\n got %s\nwant %s", got, r["canonical"])
			}
			if SHA256Hex(r["in"]) != r["sha256"] {
				t.Errorf("sha mismatch for %s", got)
			}
		case "template":
			m := r["in"].(map[string]any)
			if got := SemanticSHA(m); got != r["sha256"] {
				t.Errorf("%v: semantic sha %s, server %s", m["id"], got, r["sha256"])
			}
		}
	}
}

func TestParamsParity(t *testing.T) {
	rows := jsonl(t, "act_params_parity.jsonl")
	ctx := &ParamCtx{Host: "web-prod-07"}
	for _, r := range rows {
		spec, _ := r["spec"].(map[string]any)
		got, err := Validate(spec, r["value"], false, ctx)
		ok, _ := r["ok"].(bool)
		if ok {
			if err != nil {
				t.Errorf("%v %#v: server accepts, actuator refuses: %v", spec, r["value"], err)
				continue
			}
			if string(MustCanonical(got)) != string(MustCanonical(r["out"])) {
				t.Errorf("%v %#v: value %s, server %s", spec, r["value"], MustCanonical(got), MustCanonical(r["out"]))
			}
			continue
		}
		if err == nil {
			t.Errorf("%v %#v: server refuses (%v), actuator accepts %v", spec, r["value"], r["code"], got)
		} else if ErrCode(err) != r["code"] {
			t.Errorf("%v %#v: code %s, server %s", spec, r["value"], ErrCode(err), r["code"])
		}
	}
}

func TestPrivacyParity(t *testing.T) {
	rows := jsonl(t, "privacy_parity.jsonl")
	ps := &Pseudonymiser{key: []byte("parity-fixture-key")} // the key the fixture was written with
	for _, r := range rows {
		if r["kind"] == "text" {
			if got := ScrubStrict(r["in"].(string)); got != r["out"] {
				t.Errorf("scrub_strict(%q) = %q, server %q", r["in"], got, r["out"])
			}
			continue
		}
		schema, _ := r["schema"].(map[string]any)
		got := ScrubFields(r["in"], schema, ps)
		if string(MustCanonical(got)) != string(MustCanonical(r["out"])) {
			t.Errorf("scrub_fields(%v) = %s, server %s", r["in"], MustCanonical(got), MustCanonical(r["out"]))
		}
	}
}

func TestCanariesNeverSurvive(t *testing.T) {
	b, err := os.ReadFile(fixture(t, "privacy_canaries.txt"))
	if err != nil {
		t.Fatal(err)
	}
	out := ScrubStrict(string(b))
	for _, c := range []string{"Canary-Pg-7f3a", "Canary-Ora-91", "Canary-Env-22", "Canary-Ks-5", "Canary-Flag-8", "Canary-Oidc-3",
		"AKIACANARY0000000001", "CanarySess0123456789abcdef", "MIIEowIBAAKCAQEAcanaryRSAprivateKeyBodyLine0123456789"} {
		if bytes.Contains([]byte(out), []byte(c)) {
			t.Errorf("canary %s survived", c)
		}
	}
}

func TestEmbeddedPacksMatchVendor(t *testing.T) {
	vendor := filepath.Join("..", "..", "..", "agent_actions")
	if _, err := os.Stat(vendor); err != nil {
		t.Skip("agent_actions/ not present")
	}
	for _, n := range PackNames() {
		emb, _ := packFS.ReadFile("packs/" + n + ".tmpl.json")
		src, err := os.ReadFile(filepath.Join(vendor, n+".tmpl.json"))
		if err != nil || !bytes.Equal(emb, src) {
			t.Errorf("goagent/internal/act/packs/%s.tmpl.json differs from agent_actions/ (copy it again)", n)
		}
	}
	emb, _ := packFS.ReadFile("packs/scripts/lsof_deleted.sh")
	src, _ := os.ReadFile(filepath.Join(vendor, "scripts", "lsof_deleted.sh"))
	if !bytes.Equal(emb, src) {
		t.Error("embedded lsof_deleted.sh differs from agent_actions/scripts/")
	}
}

func TestRFC8032Vectors(t *testing.T) {
	var d struct {
		Vectors []struct{ Name, Secret, Public, Message, Signature string }
	}
	b, err := os.ReadFile(fixture(t, "rfc8032_ed25519.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := Decode(b, &d); err != nil || len(d.Vectors) < 3 {
		t.Fatalf("vectors: %v", err)
	}
	for _, v := range d.Vectors {
		seed, _ := hex.DecodeString(v.Secret)
		msg, _ := hex.DecodeString(v.Message)
		key := ed25519.NewKeyFromSeed(seed)
		if got := hex.EncodeToString(key.Public().(ed25519.PublicKey)); got != v.Public {
			t.Errorf("%s: public %s", v.Name, got)
		}
		sig := ed25519.Sign(key, msg)
		if got := hex.EncodeToString(sig); got != v.Signature {
			t.Errorf("%s: signature %s", v.Name, got)
		}
		pub, _ := hex.DecodeString(v.Public)
		if !VerifyRaw([]ed25519.PublicKey{pub}, msg, encodeB64(sig)) {
			t.Errorf("%s: VerifyRaw refused the vector", v.Name)
		}
		bad := append([]byte(nil), msg...)
		bad = append(bad, 0)
		if VerifyRaw([]ed25519.PublicKey{pub}, bad, encodeB64(sig)) {
			t.Errorf("%s: a changed message verified", v.Name)
		}
	}
}
