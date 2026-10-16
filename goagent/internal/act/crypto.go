package act

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// Signatures of the job channel (§9.3.1): Ed25519 over "wo-<purpose>-v1\n" + canonical(body), purposes job, result, hello,
// req, anchor, attest, rotate. The actuator holds its own key (agent.key, 0600, created on first start); the dashboard's
// dispatch and anchor keys live in watchover-keyd and are pinned here as dispatch.pub / anchor.pub.

// PubString is the "ed25519:<base64>" form used on the wire and in dispatch.pub / anchor.pub.
func PubString(pub ed25519.PublicKey) string {
	return "ed25519:" + base64.StdEncoding.EncodeToString(pub)
}

// ParsePub reads "ed25519:<base64>" (whitespace ignored).
func ParsePub(s string) (ed25519.PublicKey, error) {
	s = strings.TrimSpace(s)
	b64, ok := strings.CutPrefix(s, "ed25519:")
	if !ok {
		return nil, errors.New("not an ed25519:<base64> key")
	}
	raw, err := base64.StdEncoding.DecodeString(b64)
	if err != nil || len(raw) != ed25519.PublicKeySize {
		return nil, errors.New("bad ed25519 public key")
	}
	return ed25519.PublicKey(raw), nil
}

// Fingerprint is "SHA256:<base64(sha256(raw public key)) without padding>", what people compare (keyd.fingerprint).
func Fingerprint(pub ed25519.PublicKey) string {
	h := sha256.Sum256(pub)
	return "SHA256:" + strings.TrimRight(base64.StdEncoding.EncodeToString(h[:]), "=")
}

// Kid is the short key id the dashboard uses ("d-" + 8 characters of the fingerprint for dispatch keys).
func Kid(prefix string, pub ed25519.PublicKey) string {
	return prefix + Fingerprint(pub)[7:15]
}

// SignPurpose signs "wo-<purpose>-v1\n" + canonical(body) and returns base64.
func SignPurpose(key ed25519.PrivateKey, purpose string, body any) (string, error) {
	c, err := Canonical(body)
	if err != nil {
		return "", err
	}
	msg := append([]byte("wo-"+purpose+"-v1\n"), c...)
	return base64.StdEncoding.EncodeToString(ed25519.Sign(key, msg)), nil
}

// VerifyPurpose checks a base64 signature over "wo-<purpose>-v1\n" + canonical(body) with any of the keys.
func VerifyPurpose(keys []ed25519.PublicKey, purpose string, body any, sigB64 string) bool {
	c, err := Canonical(body)
	if err != nil {
		return false
	}
	return VerifyRaw(keys, append([]byte("wo-"+purpose+"-v1\n"), c...), sigB64)
}

// VerifyRaw checks a base64 signature over msg with any of the keys.
func VerifyRaw(keys []ed25519.PublicKey, msg []byte, sigB64 string) bool {
	sig, err := base64.StdEncoding.DecodeString(strings.TrimSpace(sigB64))
	if err != nil || len(sig) != ed25519.SignatureSize {
		return false
	}
	for _, k := range keys {
		if len(k) == ed25519.PublicKeySize && ed25519.Verify(k, msg, sig) {
			return true
		}
	}
	return false
}

// LoadOrCreateKey reads state/agent.key (base64 seed, 0600) or creates it. created reports a new key (print the fingerprint).
func LoadOrCreateKey(stateDir string) (key ed25519.PrivateKey, created bool, err error) {
	p := filepath.Join(stateDir, "agent.key")
	if b, rerr := os.ReadFile(p); rerr == nil {
		seed, derr := base64.StdEncoding.DecodeString(strings.TrimSpace(string(b)))
		if derr != nil || len(seed) != ed25519.SeedSize {
			return nil, false, fmt.Errorf("%s is not an Ed25519 seed", p)
		}
		return ed25519.NewKeyFromSeed(seed), false, nil
	} else if !os.IsNotExist(rerr) {
		return nil, false, rerr
	}
	if err := os.MkdirAll(stateDir, 0o700); err != nil {
		return nil, false, err
	}
	seed := make([]byte, ed25519.SeedSize)
	if _, err := rand.Read(seed); err != nil {
		return nil, false, err
	}
	f, err := os.OpenFile(p, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return nil, false, err
	}
	if _, err := f.WriteString(base64.StdEncoding.EncodeToString(seed) + "\n"); err != nil {
		f.Close()
		return nil, false, err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return nil, false, err
	}
	if err := f.Close(); err != nil {
		return nil, false, err
	}
	return ed25519.NewKeyFromSeed(seed), true, nil
}

// ReadPubFile reads every "ed25519:<base64>" line of a pinned key file (dispatch.pub / anchor.pub).
func ReadPubFile(p string) ([]ed25519.PublicKey, error) {
	b, err := os.ReadFile(p)
	if err != nil {
		return nil, err
	}
	var out []ed25519.PublicKey
	for _, ln := range strings.Split(string(b), "\n") {
		ln = strings.TrimSpace(ln)
		if ln == "" || strings.HasPrefix(ln, "#") {
			continue
		}
		k, err := ParsePub(ln)
		if err != nil {
			return nil, fmt.Errorf("%s: %v", p, err)
		}
		out = append(out, k)
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("%s holds no key", p)
	}
	return out, nil
}

func encodeB64(b []byte) string { return base64.StdEncoding.EncodeToString(b) }
