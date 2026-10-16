package act

import (
	"embed"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// The vendor packs travel inside the wo-agent binary (whose sha256 the installer checks), so the host allowlist is
// installed from the same artefact that runs it; the dashboard never pushes or changes it afterwards (§5.1). The copies
// under packs/ are kept equal to agent_actions/ by TestEmbeddedPacksMatchVendor.
//
//go:embed packs/*.tmpl.json packs/scripts/*
var packFS embed.FS

// PackNames are the embedded packs ("linux-diag", "linux-act", …).
func PackNames() []string {
	ents, _ := fs.ReadDir(packFS, "packs")
	var out []string
	for _, e := range ents {
		if n, ok := strings.CutSuffix(e.Name(), ".tmpl.json"); ok {
			out = append(out, n)
		}
	}
	sort.Strings(out)
	return out
}

// InstallOptions of `wo-agent act install`.
type InstallOptions struct {
	ActionsDir, ScriptsDir, StateDir string
	Packs                            []string
	MaxRisk                          string // local.json max_risk for a new local.json (default R0)
	Criticality                      string // critical / high force max_risk R0 (K4)
	DispatchPub, AnchorPub           string // pins from the enrolment answer / install line ("" = pin on first use)
	Token                            string // actuator token (wa_…)
	Sim                              bool   // sandbox: no root ownership required
}

func writeRootFile(p string, data []byte, mode os.FileMode, sim bool) error {
	if err := writeFileDurable(p, data); err != nil {
		return err
	}
	if err := os.Chmod(p, mode); err != nil {
		return err
	}
	if !sim && os.Geteuid() == 0 {
		return os.Chown(p, 0, 0)
	}
	return nil
}

// Install writes the allowlist (packs and scripts), a local.json when there is none (never overwritten: it is the host
// administrator's), the pinned keys, the token and the state directory with reserve.bin.
func Install(o InstallOptions) ([]string, error) {
	var notes []string
	if !o.Sim && os.Geteuid() != 0 {
		return nil, errors.New("run as root: the allowlist must be root-owned")
	}
	for _, d := range []string{o.ActionsDir, o.ScriptsDir} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			return nil, err
		}
		_ = os.Chmod(d, 0o755)
	}
	if err := os.MkdirAll(o.StateDir, 0o700); err != nil {
		return nil, err
	}
	_ = os.Chmod(o.StateDir, 0o700)
	known := map[string]bool{}
	for _, n := range PackNames() {
		known[n] = true
	}
	for _, p := range o.Packs {
		p = strings.TrimSpace(p)
		if p == "" {
			continue
		}
		if !known[p] {
			return nil, fmt.Errorf("unknown pack %q (embedded: %s)", p, strings.Join(PackNames(), ", "))
		}
		b, _ := packFS.ReadFile("packs/" + p + ".tmpl.json")
		if err := writeRootFile(filepath.Join(o.ActionsDir, p+".tmpl.json"), b, 0o644, o.Sim); err != nil {
			return nil, err
		}
		notes = append(notes, "pack "+p)
	}
	scripts, _ := fs.ReadDir(packFS, "packs/scripts")
	for _, s := range scripts {
		b, _ := packFS.ReadFile("packs/scripts/" + s.Name())
		if err := writeRootFile(filepath.Join(o.ScriptsDir, s.Name()), b, 0o755, o.Sim); err != nil {
			return nil, err
		}
	}
	lp := filepath.Join(o.ActionsDir, "local.json")
	if _, err := os.Stat(lp); os.IsNotExist(err) {
		pol := DefaultPolicy()
		if riskRankOf(o.MaxRisk) >= 0 {
			pol.MaxRisk = o.MaxRisk
		}
		if o.Criticality == "critical" || o.Criticality == "high" {
			pol.MaxRisk = "R0"
		}
		b, _ := json.MarshalIndent(pol, "", "  ")
		if err := writeRootFile(lp, append(b, '\n'), 0o644, o.Sim); err != nil {
			return nil, err
		}
		notes = append(notes, "local.json max_risk "+pol.MaxRisk)
	} else {
		notes = append(notes, "local.json kept")
	}
	pinned := false
	for name, v := range map[string]string{"dispatch.pub": o.DispatchPub, "anchor.pub": o.AnchorPub} {
		if v == "" {
			continue
		}
		if _, err := ParsePub(v); err != nil {
			return nil, fmt.Errorf("%s: %v", name, err)
		}
		p := filepath.Join(o.ActionsDir, name)
		if old, err := os.ReadFile(p); err == nil && strings.TrimSpace(string(old)) != strings.TrimSpace(v) {
			return nil, fmt.Errorf("%s already pins another key: remove it by hand to re-pin", p)
		}
		if err := writeRootFile(p, []byte(strings.TrimSpace(v)+"\n"), 0o644, o.Sim); err != nil {
			return nil, err
		}
		pinned = true
	}
	if pinned {
		_ = writeFileDurable(filepath.Join(o.StateDir, "pins.json"), MustCanonical(map[string]any{"source": "install"}))
	}
	if o.Token != "" {
		if !strings.HasPrefix(o.Token, "wa_") {
			return nil, errors.New("the actuator token starts with wa_")
		}
		if err := writeFileDurable(filepath.Join(o.StateDir, "actuator.token"), []byte(o.Token+"\n")); err != nil {
			return nil, err
		}
	}
	if err := EnsureReserve(o.StateDir); err != nil {
		notes = append(notes, "reserve.bin: "+err.Error())
	}
	return notes, nil
}

// Writes is the union of the installed templates' writes (systemd ReadWritePaths=) plus nothing else.
func Writes(actionsDir string) []string {
	al := LoadAllowlist(actionsDir, "", true, "linux")
	set := map[string]bool{}
	for _, t := range al.Templates {
		for _, w := range t.Strings("writes") {
			set[w] = true
		}
	}
	var out []string
	for w := range set {
		out = append(out, w)
	}
	sort.Strings(out)
	return out
}
