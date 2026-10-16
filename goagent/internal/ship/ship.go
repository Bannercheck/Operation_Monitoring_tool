// Package ship is wo-agent's log-shipping role (`wo-agent ship`, the default role): agent.py's collector for Linux in Go,
// standard library only. Same wire format to /ingest (one POST per batch, headers X-Agent / X-File-Name / X-Agent-Version,
// bearer token, newline-framed body, the same JSON lines for hello / metrics / flows / discovery / journal), same
// configuration (agent.py's flags and WATCHOVER_* variables, so the installer's agent.env serves both), same discovery
// rules and the same spool format. Additions: positions on disk (restarts neither re-send nor lose lines), a journal
// cursor, Retry-After honoured, a byte cap on the spool, a clean shutdown that hands over what was read.
package ship

import (
	"context"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

// Version of the log-shipping role (agent.py is 0.6.0); sent as X-Agent-Version.
const Version = "0.7.0"

func getenv(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func truthy(k string) bool {
	switch strings.ToLower(os.Getenv(k)) {
	case "1", "true", "yes":
		return true
	}
	return false
}

type multi []string

func (m *multi) String() string     { return strings.Join(*m, ",") }
func (m *multi) Set(v string) error { *m = append(*m, v); return nil }

// Config is agent.py's command line plus wo-agent's additions (positions file, spool cap, seeding).
type Config struct {
	URL, Key, Agent, CA, Env, Spool, State, File string
	Tails                                        []string
	Insecure, Auto, Discover, Journal, Metrics   bool
	Flows, Test, Seed                            bool
	Interval                                     float64
	SpoolMaxMB                                   int64
}

func parse(args []string, stderr io.Writer) (*Config, *flag.FlagSet, error) {
	host, _ := os.Hostname()
	c := &Config{}
	fs := flag.NewFlagSet("ship", flag.ContinueOnError)
	fs.SetOutput(stderr)
	fs.StringVar(&c.URL, "url", getenv("WATCHOVER_URL", "http://localhost:8600/ingest"), "receiver URL (…/ingest); env WATCHOVER_URL")
	fs.StringVar(&c.Key, "key", os.Getenv("WATCHOVER_TOKEN"), "this agent's token (Connection settings › Agents); env WATCHOVER_TOKEN")
	fs.StringVar(&c.Key, "token", os.Getenv("WATCHOVER_TOKEN"), "same as --key")
	fs.StringVar(&c.Agent, "agent", getenv("WATCHOVER_AGENT", host), "name sent as X-Agent (a registered token overrides it)")
	fs.StringVar(&c.CA, "ca", os.Getenv("WATCHOVER_CA"), "CA certificate (PEM) to trust for an https receiver; env WATCHOVER_CA")
	fs.BoolVar(&c.Insecure, "insecure", false, "skip TLS certificate checks (testing only)")
	fs.StringVar(&c.Env, "env", os.Getenv("AGENT_ENV"), "environment tag for this host (a registered token overrides it)")
	tails := multi{}
	fs.Var(&tails, "tail", "follow a log file or glob; repeatable (env WATCHOVER_LOGS, comma separated)")
	fs.BoolVar(&c.Auto, "auto", false, "discover the applications on this host and follow their logs; env WATCHOVER_LOGS=auto")
	fs.BoolVar(&c.Discover, "discover", false, "print what --auto would follow and exit")
	fs.BoolVar(&c.Journal, "journal", false, "also stream the systemd journal")
	winev := fs.Bool("windows-events", false, "accepted for agent.py compatibility (Windows only: use agent.py there)")
	fs.BoolVar(&c.Metrics, "metrics", false, "ship this host's CPU / memory / disk / GPU every --interval seconds")
	fs.BoolVar(&c.Flows, "flows", false, "ship this host's TCP connections every 60 s (the service map's traffic edges)")
	fs.StringVar(&c.File, "file", "", "send a file once and exit")
	fs.BoolVar(&c.Test, "test", false, "send one hello event, print the receiver's answer, exit")
	iv, _ := strconv.ParseFloat(getenv("WATCHOVER_INTERVAL", "10"), 64)
	fs.Float64Var(&c.Interval, "interval", iv, "metrics interval in seconds; env WATCHOVER_INTERVAL")
	fs.StringVar(&c.Spool, "spool", os.Getenv("WATCHOVER_SPOOL"), "directory for undeliverable batches (re-sent when the receiver is back)")
	mb, _ := strconv.ParseInt(getenv("WATCHOVER_SPOOL_MAX_MB", "512"), 10, 64)
	fs.Int64Var(&c.SpoolMaxMB, "spool-max-mb", mb, "spool size cap in MB (the oldest batches give way); env WATCHOVER_SPOOL_MAX_MB")
	fs.StringVar(&c.State, "state", os.Getenv("WATCHOVER_STATE"), "positions file (default: positions.json next to the spool); env WATCHOVER_STATE")
	fs.BoolVar(&c.Seed, "seed-positions", false, "record every followed file at its current end and exit (hand-over from agent.py)")
	simulate := fs.Bool("simulate", false, "not in wo-agent: use agent.py --simulate from the dashboard checkout")
	if err := fs.Parse(args); err != nil {
		return nil, fs, err
	}
	if *simulate {
		return nil, fs, fmt.Errorf("--simulate needs the dashboard checkout: use python3 agent.py --simulate")
	}
	_ = winev
	c.Tails = append(c.Tails, tails...)
	for _, x := range strings.Split(os.Getenv("WATCHOVER_LOGS"), ",") {
		switch x = strings.TrimSpace(x); x {
		case "":
		case "auto":
			c.Auto = true
		case "journal":
			c.Journal = true
		case "windows-events":
		default:
			c.Tails = append(c.Tails, x)
		}
	}
	if truthy("WATCHOVER_METRICS") {
		c.Metrics = true
	}
	if truthy("WATCHOVER_FLOWS") {
		c.Flows = true
	}
	if c.State == "" && c.Spool != "" {
		c.State = filepath.Join(filepath.Dir(filepath.Clean(c.Spool)), "positions.json")
	}
	return c, fs, nil
}

// patterns is agent.py's tail list: --tail / WATCHOVER_LOGS, then the discovered files (capped), without duplicates; and
// whether the journal should be added (--auto on a systemd host without /var/log/syslog or /var/log/messages).
func (c *Config) patterns(found []Found) ([]string, bool) {
	tails := append([]string(nil), c.Tails...)
	journal := c.Journal
	if c.Auto {
		var auto []string
		for _, f := range found {
			auto = append(auto, f.Files...)
		}
		if len(auto) > MaxAutoFiles {
			auto = auto[:MaxAutoFiles]
		}
		have := map[string]bool{}
		for _, t := range tails {
			have[t] = true
		}
		for _, p := range auto {
			if !have[p] {
				tails = append(tails, p)
				have[p] = true
			}
		}
		if !have["/var/log/syslog"] && !have["/var/log/messages"] {
			if fi, err := os.Stat("/run/systemd/system"); err == nil && fi.IsDir() {
				journal = true
			}
		}
	}
	return tails, journal
}

func helloBody(agent, env string, now time.Time) []byte {
	host, _ := os.Hostname()
	return append(obj{{"ts", isoUTC(now)}, {"level", "info"}, {"service", "watchover-agent"}, {"host", agent}, {"env", env},
		{"msg", fmt.Sprintf("agent wo-agent %s started on %s", Version, host)}}.bytes(true), '\n')
}

// Main runs the role with agent.py's command line; the exit codes are agent.py's (2: test / upload failed or usage).
func Main(args []string) int {
	stdout, stderr := os.Stdout, os.Stderr
	logf := func(format string, a ...any) { fmt.Fprintf(stderr, format+"\n", a...) }
	c, fs, err := parse(args, stderr)
	if err != nil {
		if err != flag.ErrHelp {
			fmt.Fprintln(stderr, "wo-agent ship:", err)
		}
		return 2
	}
	if fs.NArg() > 0 {
		fmt.Fprintln(stderr, "wo-agent ship: unexpected argument", fs.Arg(0))
		return 2
	}
	if c.Discover {
		for _, f := range Discover() {
			extra := ""
			if f.Skipped > 0 {
				extra = fmt.Sprintf(" (+%d older)", f.Skipped)
			}
			fmt.Fprintf(stdout, "%s: %d files%s\n", f.App, len(f.Files), extra)
			for _, p := range f.Files {
				fmt.Fprintf(stdout, "    %s\n", p)
			}
		}
		return 0
	}
	var found []Found
	if c.Auto {
		found = Discover()
	}
	tails, journal := c.patterns(found)
	if c.Seed {
		if c.State == "" {
			fmt.Fprintln(stderr, "wo-agent ship --seed-positions: no positions file (--state or --spool)")
			return 2
		}
		t := &Tailer{Patterns: tails, State: c.State, Agent: c.Agent}
		n, err := t.Seed()
		switch {
		case err != nil:
			fmt.Fprintln(stderr, "wo-agent ship --seed-positions:", err)
			return 1
		case n < 0:
			fmt.Fprintln(stdout, "positions kept: "+c.State)
		default:
			fmt.Fprintf(stdout, "positions seeded: %d files at their current end → %s\n", n, c.State)
		}
		return 0
	}
	client, err := NewClient(c.CA, c.Insecure, 15*time.Second)
	if err != nil {
		fmt.Fprintln(stderr, "wo-agent ship:", err)
		return 2
	}
	snd := &Sender{URL: c.URL, Key: c.Key, Agent: c.Agent, Version: Version, Client: client, Logf: logf}
	if c.Test { // no spool: a failed hello must not linger in it
		res, out := snd.SendAnswer(context.Background(), helloBody(c.Agent, c.Env, time.Now()), "agent.jsonl")
		if res != Delivered {
			fmt.Fprintln(stdout, "FAILED: the receiver did not accept the hello event (address, port or token)")
			return 2
		}
		fmt.Fprintf(stdout, "OK: receiver accepted · identity=%v · agent=%v\n", pyStrOf(out["identity"]), pyStrOf(out["agent"]))
		return 0
	}
	if c.Spool != "" {
		sp, err := OpenSpool(c.Spool, 2000, c.SpoolMaxMB<<20)
		if err != nil {
			fmt.Fprintln(stderr, "wo-agent ship: spool:", err)
			return 2
		}
		snd.Spool = sp
	}
	if c.File != "" {
		b, err := os.ReadFile(c.File)
		if err != nil {
			fmt.Fprintln(stderr, "wo-agent ship:", err)
			return 2
		}
		res, out := snd.SendAnswer(context.Background(), b, filepath.Base(c.File))
		if res != Delivered {
			fmt.Fprintln(stdout, "failed")
			return 2
		}
		fmt.Fprintln(stdout, "ok", out)
		return 0
	}
	if len(tails) == 0 && !c.Metrics && !journal {
		fmt.Fprintln(stderr, "wo-agent ship: nothing to ship: use --metrics, --auto and/or --tail PATH (or --test / --file)")
		return 2
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	return run(ctx, c, snd, tails, journal, found, logf, stdout)
}

func pyStrOf(v any) string {
	if v == nil {
		return "None"
	}
	return fmt.Sprint(v)
}

func run(ctx context.Context, c *Config, snd *Sender, tails []string, journal bool, found []Found, logf func(string, ...any), stdout io.Writer) int {
	host, _ := os.Hostname()
	go snd.Drain(ctx)
	snd.Send(ctx, helloBody(c.Agent, c.Env, time.Now()), "agent.jsonl")
	if len(found) > 0 {
		snd.Send(ctx, discoveryBody(found, c.Agent, c.Env, time.Now()), "discovery.jsonl")
	}
	var wg sync.WaitGroup
	if c.Metrics {
		wg.Add(1)
		go func() {
			defer wg.Done()
			mp := &metricsProbe{}
			every := time.Duration(max(c.Interval, 1) * float64(time.Second))
			for {
				snd.Send(ctx, mp.line(host, c.Env, time.Now()), "metrics.jsonl")
				select {
				case <-ctx.Done():
					return
				case <-time.After(every):
				}
			}
		}()
	}
	if c.Flows {
		wg.Add(1)
		go func() {
			defer wg.Done()
			fp := &flowProbe{}
			for {
				if b := flowsBody(fp.flows(), host, c.Env, time.Now()); b != nil {
					snd.Send(ctx, b, "flows.jsonl")
				}
				select {
				case <-ctx.Done():
					return
				case <-time.After(60 * time.Second):
				}
			}
		}()
	}
	if len(tails) > 0 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			t := &Tailer{Patterns: tails, State: c.State, Agent: c.Agent, Send: snd.Send, Logf: logf}
			t.Run(ctx)
		}()
	}
	if journal {
		wg.Add(1)
		go func() {
			defer wg.Done()
			cur := ""
			if c.State != "" {
				cur = filepath.Join(filepath.Dir(c.State), "journal.cursor")
			}
			(&Journal{Send: snd.Send, Agent: c.Agent, CursorFile: cur, Logf: logf}).Run(ctx)
		}()
	}
	apps := "-"
	if len(found) > 0 {
		var parts []string
		for _, f := range found {
			parts = append(parts, fmt.Sprintf("%s (%d)", f.App, len(f.Files)))
		}
		apps = strings.Join(parts, ", ")
	}
	onoff := map[bool]string{true: "on", false: "off"}
	sp := c.Spool
	if sp == "" {
		sp = "off"
	}
	fmt.Fprintf(stdout, "watchover agent %s (wo-agent) -> %s · metrics=%s · tails=%d · auto=%s · spool=%s · positions=%s\n",
		Version, c.URL, onoff[c.Metrics], len(tails), apps, sp, map[bool]string{true: c.State, false: "off"}[c.State != ""])
	tick := time.NewTicker(60 * time.Second)
	defer tick.Stop()
	for {
		select {
		case <-tick.C:
			logf("%s", snd.Stats())
		case <-ctx.Done():
			done := make(chan struct{})
			go func() { wg.Wait(); close(done) }()
			select {
			case <-done:
			case <-time.After(25 * time.Second):
				logf("shutdown: workers did not stop in time")
			}
			logf("stopped: %s", snd.Stats())
			return 0
		}
	}
}
