// Package update is wo-agent's self-update (`wo-agent update`, run by the root-owned oneshot unit watchover-update.service on
// a timer and when the receiver hints a newer release). Trust chain: keyd (the dashboard's key service) signs the manifest
// of the release it finds in its own image with release.key; the installer pins that key on the host (WATCHOVER_RELEASE_KEY,
// from the --release-key fingerprint in the install command, or the https answer); this package accepts a binary only when
// the manifest verifies against the pinned key, the binary's sha256 / size match the signed manifest, its compiled-in
// version is the one the manifest names, and that version is newer than the running one, unless a pin signed by the same
// key and bound to this host asks for exactly that version (the dashboard's "bu sürüme döndür").
package update

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// Signed is keyd's envelope: Body is the canonical JSON (base64), Sig the Ed25519 signature over the domain prefix + Body's
// bytes, Key the signer's public key (informational: verification always uses the pinned key).
type Signed struct {
	Body    string `json:"body"`
	Sig     string `json:"sig"`
	Key     string `json:"key,omitempty"`
	Version string `json:"version,omitempty"`
}

// FileSum is one file of a release.
type FileSum struct {
	SHA256 string `json:"sha256"`
	Size   int64  `json:"size"`
}

// Release is a signed release manifest (keyd release()).
type Release struct {
	V       int                `json:"v"`
	Seq     int64              `json:"seq"`
	Version string             `json:"version"`
	Issued  string             `json:"issued"`
	Files   map[string]FileSum `json:"files"`
}

// Pin is a signed, host-bound, expiring order to run one version (keyd sign_pin()).
type Pin struct {
	V         int                `json:"v"`
	Host      string             `json:"host"`
	Version   string             `json:"version"`
	Seq       int64              `json:"seq"`
	Issued    string             `json:"issued"`
	ExpiresAt string             `json:"expires_at"`
	Files     map[string]FileSum `json:"files"`
}

const (
	releaseDomain = "wo-release-v1\n"
	pinDomain     = "wo-pin-v1\n"
)

// ParseKey reads "ed25519:<base64 raw key>".
func ParseKey(s string) (ed25519.PublicKey, error) {
	s = strings.TrimSpace(s)
	b64, ok := strings.CutPrefix(s, "ed25519:")
	if !ok {
		return nil, errors.New("release key must look like ed25519:<base64>")
	}
	raw, err := base64.StdEncoding.DecodeString(b64)
	if err != nil || len(raw) != ed25519.PublicKeySize {
		return nil, errors.New("release key is not a 32-byte Ed25519 key")
	}
	return ed25519.PublicKey(raw), nil
}

// Fingerprint is keyd's form: "SHA256:" + base64 (no padding) of sha256(raw key), the value the install command carries.
func Fingerprint(pub ed25519.PublicKey) string {
	h := sha256.Sum256(pub)
	return "SHA256:" + base64.RawStdEncoding.EncodeToString(h[:])
}

func open(s Signed, domain string, key ed25519.PublicKey) ([]byte, error) {
	if len(key) != ed25519.PublicKeySize {
		return nil, errors.New("no release key pinned on this host")
	}
	body, err := base64.StdEncoding.DecodeString(s.Body)
	if err != nil || len(body) == 0 {
		return nil, errors.New("signed document has no body")
	}
	sig, err := base64.StdEncoding.DecodeString(s.Sig)
	if err != nil || len(sig) != ed25519.SignatureSize {
		return nil, errors.New("signed document has no valid signature")
	}
	if !ed25519.Verify(key, append([]byte(domain), body...), sig) {
		return nil, errors.New("signature does not verify against the pinned release key")
	}
	return body, nil
}

var hexSHA = regexp.MustCompile(`^[0-9a-f]{64}$`)

func checkFiles(files map[string]FileSum) error {
	for n, f := range files {
		if !hexSHA.MatchString(f.SHA256) || f.Size <= 0 {
			return fmt.Errorf("manifest entry %s is malformed", n)
		}
	}
	return nil
}

// VerifyRelease checks the signature with the pinned key and returns the manifest.
func VerifyRelease(s Signed, key ed25519.PublicKey) (*Release, error) {
	body, err := open(s, releaseDomain, key)
	if err != nil {
		return nil, err
	}
	var r Release
	if err := json.Unmarshal(body, &r); err != nil {
		return nil, fmt.Errorf("release manifest: %v", err)
	}
	if r.V != 1 || !versionRe.MatchString(r.Version) || len(r.Files) == 0 {
		return nil, errors.New("release manifest is incomplete")
	}
	return &r, checkFiles(r.Files)
}

// VerifyPin checks a pin: signature, this host, not expired.
func VerifyPin(s Signed, key ed25519.PublicKey, host string, now time.Time) (*Pin, error) {
	body, err := open(s, pinDomain, key)
	if err != nil {
		return nil, err
	}
	var p Pin
	if err := json.Unmarshal(body, &p); err != nil {
		return nil, fmt.Errorf("pin: %v", err)
	}
	if p.V != 1 || !versionRe.MatchString(p.Version) || len(p.Files) == 0 {
		return nil, errors.New("pin is incomplete")
	}
	if !strings.EqualFold(p.Host, host) {
		return nil, fmt.Errorf("pin is for %q, not for this host (%q)", p.Host, host)
	}
	exp, err := time.Parse(time.RFC3339, p.ExpiresAt)
	if err != nil || !now.Before(exp) {
		return nil, errors.New("pin has expired")
	}
	return &p, checkFiles(p.Files)
}

var versionRe = regexp.MustCompile(`^[0-9]+\.[0-9]+\.[0-9]+[0-9A-Za-z.+-]*$`)

// markerRe matches ship.Marker. The prefix is built at run time so that this pattern's own text in the binary never matches.
var markerRe = regexp.MustCompile(strings.ToUpper("wo-agent-version=") + `([0-9]+\.[0-9]+\.[0-9]+[0-9A-Za-z.+-]*);`)

// VersionOf reads the release version compiled into a wo-agent binary (ship.Marker).
func VersionOf(b []byte) (string, error) {
	found := map[string]bool{}
	for _, m := range markerRe.FindAllSubmatch(b, -1) {
		found[string(m[1])] = true
	}
	if len(found) != 1 {
		return "", fmt.Errorf("binary carries %d version markers (want exactly one)", len(found))
	}
	for v := range found {
		return v, nil
	}
	return "", nil
}

// Compare orders versions numerically by major.minor.patch; a pre-release suffix (1.2.3-rc1) sorts before the plain release.
func Compare(a, b string) int {
	pa, sa := split(a)
	pb, sb := split(b)
	for i := 0; i < 3; i++ {
		if pa[i] != pb[i] {
			if pa[i] < pb[i] {
				return -1
			}
			return 1
		}
	}
	switch {
	case sa == sb:
		return 0
	case sa == "":
		return 1
	case sb == "":
		return -1
	}
	return strings.Compare(sa, sb)
}

func split(v string) ([3]int, string) {
	var n [3]int
	v = strings.TrimSpace(v)
	core, suffix := v, ""
	if i := strings.IndexAny(v, "-+"); i >= 0 {
		core, suffix = v[:i], v[i:]
	}
	for i, p := range strings.SplitN(core, ".", 3) {
		n[i], _ = strconv.Atoi(p)
	}
	return n, suffix
}

func sha256Hex(b []byte) string {
	h := sha256.Sum256(b)
	return fmt.Sprintf("%x", h[:])
}

func trimmed(b []byte) string { return string(bytes.TrimSpace(b)) }
