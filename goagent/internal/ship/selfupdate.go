package ship

import (
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"sync/atomic"
	"time"

	"watchover/goagent/internal/update"
)

// selfUpdate wires the ship role into the self-update (goagent/internal/update) when the installer set it up, i.e. the update
// state directory exists (root + systemd installs; WO_UPDATE_DIR, default /var/lib/watchover/update):
//   - the first 2xx answer writes hello.json: the update gate's proof that this binary reached the receiver;
//   - every request carries X-Agent-Update (state.json: phase;to;from;at;error), so the dashboard shows updating / failed /
//     rolled back without parsing logs;
//   - an answer with X-Watchover-Update (the receiver: a newer release for this agent) starts watchover-update.service at once
//     (at most every 15 minutes) instead of waiting for its timer.
//
// This process never replaces a binary itself: the oneshot unit does, outside the services it restarts.
func selfUpdate(s *Sender, logf func(string, ...any)) {
	dir := getenv("WO_UPDATE_DIR", "/var/lib/watchover/update")
	if fi, err := os.Stat(dir); err != nil || !fi.IsDir() {
		return
	}
	var hdr atomic.Value
	readState := func() { hdr.Store(update.LoadState(dir).Header()) }
	readState()
	var helloOnce sync.Once
	var mu sync.Mutex
	var lastKick time.Time
	unit := getenv("WO_UPDATE_UNIT", "watchover-update.service")
	go func() {
		for range time.Tick(5 * time.Second) { // the gate's verdict reaches the dashboard within seconds
			readState()
		}
	}()
	s.Headers = func(h http.Header) {
		if v, _ := hdr.Load().(string); v != "" {
			h.Set("X-Agent-Update", v)
		}
	}
	s.OnAnswer = func(h http.Header) {
		helloOnce.Do(func() {
			if err := update.WriteHello(dir, Version); err != nil {
				logf("self-update: hello.json: %v", err)
			}
		})
		want := h.Get("X-Watchover-Update")
		if want == "" || want == Version {
			return
		}
		mu.Lock()
		due := time.Since(lastKick) > 15*time.Minute
		if due {
			lastKick = time.Now()
		}
		mu.Unlock()
		if !due {
			return
		}
		if _, err := os.Stat(filepath.Join(dir, "state.json")); err == nil && update.LoadState(dir).Phase == update.PhasePending {
			return
		}
		logf("self-update: the receiver offers %s (running %s): starting %s", want, Version, unit)
		go func() { _ = exec.Command("systemctl", "start", "--no-block", unit).Run() }()
	}
}
