// wo-agent is the Watchover host agent: one static binary (standard library only) for the roles a managed server runs:
// log, metric and flow shipping (`wo-agent ship`, the default role: agent.py's collector for Linux, same flags and
// WATCHOVER_* settings) and the action agent's actuator (`wo-agent act`, docs/design/action-agent.md §9.3).
//
//	wo-agent [ship] [--url U --token T --auto --metrics --flows --tail GLOB …]   follow logs, ship metrics / flows
//	wo-agent ship --test | --file F | --discover | --seed-positions             agent.py's one-shot modes, plus the hand-over
//	wo-agent enroll --url BASE --enroll-key wk_… [--name N --env E --site S --tags T]   fleet-key self-enrolment (installer)
//	wo-agent act [--url U --host H --once --sim DIR …]   pull signed jobs, run allowlisted templates, report signed results
//	wo-agent act install --packs linux-diag,linux-act …   write the root-owned allowlist, local policy, pins, token, state
//	wo-agent act writes                                     union of the installed templates' writes (systemd ReadWritePaths)
//	wo-agent act fingerprint                                the agent key's fingerprint (created on first use)
//	wo-agent act check                                      allowlist permissions and lint, exit 1 on a problem
//	wo-agent update [--url U --release-key ed25519:… --dir D]   self-update: signed release, verified, rolled back on a failed start
//	wo-agent version [--marker]
package main

import (
	"context"
	"crypto/ed25519"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"time"
	_ "time/tzdata" // local.json hours and freeze ranges name time zones; hosts may lack /usr/share/zoneinfo

	"watchover/goagent/internal/act"
	"watchover/goagent/internal/ship"
	"watchover/goagent/internal/update"
)

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: wo-agent [ship] [flags] | enroll | act [run flags] | act install | act writes | act fingerprint | act check | update | version")
	os.Exit(2)
}

func main() {
	if len(os.Args) < 2 || strings.HasPrefix(os.Args[1], "-") && os.Args[1] != "-h" && os.Args[1] != "--help" {
		os.Exit(ship.Main(os.Args[1:])) // the default role: agent.py's command line
	}
	switch os.Args[1] {
	case "version":
		if len(os.Args) > 2 && os.Args[2] == "--marker" {
			fmt.Println(ship.Marker) // the literal keyd and `wo-agent update` read from the file itself
			return
		}
		fmt.Println("wo-agent " + ship.Version + " (ship " + ship.Version + " · act " + act.Version + ")")
	case "update":
		os.Exit(updateMain(os.Args[2:]))
	case "ship":
		os.Exit(ship.Main(os.Args[2:]))
	case "enroll":
		os.Exit(enrollMain(os.Args[2:]))
	case "act":
		os.Exit(actMain(os.Args[2:]))
	default:
		usage()
	}
}

// enrollMain prints the collector token, the actuator token and the dashboard's two public keys, one per line (empty when
// not issued): the installer reads them like it read the python3 snippet's answer before.
func enrollMain(args []string) int {
	fs := flag.NewFlagSet("enroll", flag.ExitOnError)
	url := fs.String("url", "", "dashboard receiver base URL (…:8600, without /ingest)")
	key := fs.String("enroll-key", env("WATCHOVER_ENROLL_KEY", ""), "fleet enrolment key (wk_…)")
	name := fs.String("name", "", "this host's name")
	envTag := fs.String("env", "", "environment")
	site := fs.String("site", "", "site")
	tags := fs.String("tags", "", "comma separated tags")
	ca := fs.String("ca", env("WATCHOVER_CA", ""), "CA bundle for an https receiver")
	_ = fs.Parse(args)
	if *name == "" {
		*name, _ = os.Hostname()
	}
	if *url == "" || *key == "" {
		fmt.Fprintln(os.Stderr, "wo-agent enroll: --url and --enroll-key are required")
		return 2
	}
	out, err := ship.Enroll(strings.TrimSuffix(strings.TrimRight(*url, "/"), "/ingest"), *key, *name, *envTag, *site, *tags, *ca)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	for _, l := range out {
		fmt.Println(l)
	}
	return 0
}

type common struct {
	actions, state, scripts *string
}

func commonFlags(fs *flag.FlagSet) common {
	return common{
		actions: fs.String("actions-dir", env("WO_ACTIONS_DIR", "/etc/watchover/actions.d"), "root-owned allowlist and local policy"),
		state:   fs.String("state-dir", env("WO_STATE_DIR", "/var/lib/watchover/actions"), "key, ledger, manifests (0700)"),
		scripts: fs.String("scripts-dir", env("WO_SCRIPTS_DIR", "/usr/lib/watchover/actions"), "sha256-pinned scripts"),
	}
}

func actMain(args []string) int {
	sub := ""
	if len(args) > 0 && !strings.HasPrefix(args[0], "-") {
		sub, args = args[0], args[1:]
	}
	fs := flag.NewFlagSet("act", flag.ExitOnError)
	c := commonFlags(fs)
	switch sub {
	case "install":
		packs := fs.String("packs", "linux-diag,linux-act", "embedded packs to install: "+strings.Join(act.PackNames(), ", "))
		maxRisk := fs.String("max-risk", "R0", "local.json max_risk for a new local.json (R0 = diagnose only)")
		crit := fs.String("criticality", "normal", "critical / high always get max_risk R0")
		dpub := fs.String("dispatch-pub", "", "pin the dashboard's dispatch key (ed25519:…)")
		apub := fs.String("anchor-pub", "", "pin the dashboard's anchor key (ed25519:…)")
		tokenFile := fs.String("token-file", "", "file holding the actuator token (wa_…); read and copied to the state directory")
		sim := fs.Bool("sim", false, "sandbox install (no root ownership)")
		_ = fs.Parse(args)
		tok := ""
		if *tokenFile != "" {
			b, err := os.ReadFile(*tokenFile)
			if err != nil {
				fmt.Fprintln(os.Stderr, err)
				return 1
			}
			tok = strings.TrimSpace(string(b))
		}
		notes, err := act.Install(act.InstallOptions{ActionsDir: *c.actions, ScriptsDir: *c.scripts, StateDir: *c.state,
			Packs: strings.Split(*packs, ","), MaxRisk: *maxRisk, Criticality: *crit, DispatchPub: *dpub, AnchorPub: *apub, Token: tok, Sim: *sim})
		if err != nil {
			fmt.Fprintln(os.Stderr, "wo-agent act install:", err)
			return 1
		}
		for _, n := range notes {
			fmt.Println("  " + n)
		}
		return 0
	case "writes":
		_ = fs.Parse(args)
		fmt.Println(strings.Join(act.Writes(*c.actions), " "))
		return 0
	case "fingerprint":
		_ = fs.Parse(args)
		key, _, err := act.LoadOrCreateKey(*c.state)
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			return 1
		}
		fmt.Println(act.Fingerprint(key.Public().(ed25519.PublicKey)))
		return 0
	case "check":
		sim := fs.Bool("sim", false, "sandbox: only other-writable files are refused")
		_ = fs.Parse(args)
		al := act.LoadAllowlist(*c.actions, *c.scripts, *sim, "linux")
		fmt.Printf("templates: %d · allowlist permissions: %v %s · max_risk %s · enabled %v\n", len(al.Templates), al.PermsOK, al.PermsDetail,
			al.Policy.MaxRisk, al.Policy.Enabled)
		for _, e := range al.Errors {
			fmt.Println("  " + e)
		}
		if !al.PermsOK || len(al.Errors) > 0 {
			return 1
		}
		return 0
	case "", "run":
	default:
		usage()
	}
	url := fs.String("url", env("WO_URL", ""), "dashboard receiver base URL, e.g. https://edge:8600")
	host := fs.String("host", env("WO_HOST", ""), "this host's enrolled name")
	tokenFile := fs.String("token-file", env("WO_TOKEN_FILE", ""), "actuator token file (default <state-dir>/actuator.token)")
	halt := fs.String("halt-file", env("WO_HALT_FILE", "/etc/watchover/actions.halt"), "local halt switch")
	user := fs.String("default-user", env("WO_DEFAULT_USER", "wo-act"), "account for templates without run_as")
	sim := fs.String("sim", env("WO_SIM", ""), "simulator sandbox root (paths re-rooted, argv simulated)")
	ca := fs.String("ca", env("WO_CA", ""), "extra CA bundle for https")
	insecure := fs.Bool("insecure-install", env("WO_INSECURE_INSTALL", "") == "1", "installed over plain HTTP without a pinned checksum")
	once := fs.Bool("once", false, "one hello / poll / run cycle, then exit")
	_ = fs.Parse(args)
	if *host == "" {
		*host, _ = os.Hostname()
	}
	if *tokenFile == "" {
		*tokenFile = strings.TrimRight(*c.state, "/") + "/actuator.token"
	}
	syscall.Umask(0o027)
	a, err := act.New(act.Config{URL: *url, Host: *host, TokenFile: *tokenFile, ActionsDir: *c.actions, StateDir: *c.state,
		ScriptsDir: *c.scripts, HaltFile: *halt, DefaultUser: *user, Sim: *sim, CAFile: *ca, InsecureInstall: *insecure, Once: *once})
	if err != nil {
		fmt.Fprintln(os.Stderr, "wo-agent act:", err)
		return 1
	}
	if a.KeyCreated {
		fmt.Fprintln(os.Stderr, "watchover-actuator: new agent key "+a.Fingerprint()+" — compare it in Bağlantı ayarları › Eylem ajanları")
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := a.Run(ctx); err != nil {
		fmt.Fprintln(os.Stderr, "wo-agent act:", err)
		return 1
	}
	return 0
}

// updateMain is `wo-agent update`, run as root by watchover-update.service (a oneshot unit outside the services it
// restarts): on its timer and when the ship role saw the receiver hint a newer release. It never touches the actuator's
// allowlist, local policy, unit files or environment: it replaces the one binary both roles run and restarts them.
func updateMain(args []string) int {
	fs := flag.NewFlagSet("update", flag.ExitOnError)
	base := fs.String("url", env("WATCHOVER_URL", ""), "receiver URL (…/ingest or the base); env WATCHOVER_URL")
	tok := fs.String("token", env("WATCHOVER_TOKEN", ""), "this host's collector token (the receiver applies its switch, pause, rollout and pins)")
	ca := fs.String("ca", env("WATCHOVER_CA", ""), "CA bundle for an https receiver")
	key := fs.String("release-key", env("WATCHOVER_RELEASE_KEY", ""), "the pinned release key (ed25519:…), written by the installer")
	host := fs.String("name", env("WATCHOVER_AGENT_NAME", ""), "this host's enrolled name (pins are bound to it)")
	dir := fs.String("dir", env("WO_UPDATE_DIR", "/var/lib/watchover/update"), "state directory (state.json, hello.json)")
	gate := fs.Duration("gate", 120*time.Second, "how long the new binary has to reach the receiver before it is rolled back")
	units := fs.String("units", env("WO_UPDATE_UNITS", "watchover-agent,watchover-actuator"), "services to restart (the first must report back)")
	bin := fs.String("binary", "", "the binary to replace (default: this executable)")
	_ = fs.Parse(args)
	if *host == "" {
		*host, _ = os.Hostname()
	}
	if *bin == "" {
		p, err := os.Executable()
		if err == nil {
			p, err = filepath.EvalSymlinks(p)
		}
		if err != nil {
			fmt.Fprintln(os.Stderr, "wo-agent update:", err)
			return 1
		}
		*bin = p
	}
	pub, err := update.ParseKey(*key)
	if err != nil {
		fmt.Fprintln(os.Stderr, "wo-agent update: no usable release key pinned (WATCHOVER_RELEASE_KEY): re-run the install command from the Agents panel:", err)
		return 2
	}
	cl, err := ship.NewClient(*ca, false, 60*time.Second)
	if err != nil {
		fmt.Fprintln(os.Stderr, "wo-agent update:", err)
		return 1
	}
	b := strings.TrimSuffix(strings.TrimRight(*base, "/"), "/ingest")
	cfg := &update.Config{Base: b, Token: *tok, Host: *host, Arch: runtime.GOARCH, Current: ship.Version, Key: pub, Binary: *bin, Dir: *dir,
		Units: strings.Split(*units, ","), Gate: *gate, Client: cl,
		Systemctl: func(a ...string) error { return exec.Command("systemctl", a...).Run() },
		Logf:      func(f string, a ...any) { fmt.Fprintf(os.Stderr, f+"\n", a...) }}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	st, err := update.Run(ctx, cfg)
	fmt.Printf("update: %s %s%s\n", st.Phase, st.To, map[bool]string{true: " (" + st.Reason + ")", false: ""}[st.Reason != ""])
	if err != nil {
		fmt.Fprintln(os.Stderr, "wo-agent update:", err)
		return 1
	}
	if st.Phase == update.PhaseRolledBack {
		return 3
	}
	return 0
}
