package act

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

// Host facts the predicates and built-ins read: /proc, statfs, systemd. In simulator mode file system paths are re-rooted
// under the sandbox and systemd units come from <sandbox>/.sim/units.json.

// Env maps the logical paths of templates and jobs to the file system (identity on a real host).
type Env struct {
	Sim string // sandbox root; "" on a real host
}

// Real is the file system path of a logical path.
func (e *Env) Real(p string) string {
	if e == nil || e.Sim == "" {
		return p
	}
	return filepath.Join(e.Sim, filepath.Clean("/"+p))
}

// Logical is the logical path of a file system path ("" when it is outside the sandbox).
func (e *Env) Logical(p string) string {
	if e == nil || e.Sim == "" {
		return p
	}
	root := filepath.Clean(e.Sim)
	if p == root {
		return "/"
	}
	if !strings.HasPrefix(p, root+"/") {
		return ""
	}
	return p[len(root):]
}

// ---------------------------------------------------------------- processes

var (
	bootOnce sync.Once
	bootID   string
)

// BootID is the first 8 characters of /proc/sys/kernel/random/boot_id ("" when unreadable).
func BootID() string {
	bootOnce.Do(func() {
		b, err := os.ReadFile("/proc/sys/kernel/random/boot_id")
		if err == nil {
			bootID = strings.ReplaceAll(strings.TrimSpace(string(b)), "-", "")
			if len(bootID) > 8 {
				bootID = bootID[:8]
			}
		}
	})
	return bootID
}

// ProcInfo is what a pid parameter is checked against: start time (field 22 of /proc/<pid>/stat, clock ticks since boot,
// with the boot id), command name, executable, owner.
type ProcInfo struct {
	PID   int
	Start string // "<ticks>:<boot id>"
	Ticks int64
	Comm  string
	Exe   string
	UID   int
	RSSKB int64
	CPU   int64 // utime + stime ticks
}

// ReadProc reads /proc/<pid>; ok=false when the process does not exist.
func ReadProc(pid int) (ProcInfo, bool) {
	p := ProcInfo{PID: pid}
	b, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", pid))
	if err != nil {
		return p, false
	}
	s := string(b)
	l, r := strings.IndexByte(s, '('), strings.LastIndexByte(s, ')')
	if l < 0 || r < l {
		return p, false
	}
	p.Comm = s[l+1 : r]
	f := strings.Fields(s[r+1:])
	// f[0] is field 3 (state); field n is f[n-3]
	if len(f) > 21 {
		p.Ticks, _ = strconv.ParseInt(f[19], 10, 64)
		ut, _ := strconv.ParseInt(f[11], 10, 64)
		st, _ := strconv.ParseInt(f[12], 10, 64)
		p.CPU = ut + st
		rss, _ := strconv.ParseInt(f[21], 10, 64)
		p.RSSKB = rss * int64(os.Getpagesize()) / 1024
	}
	p.Start = strconv.FormatInt(p.Ticks, 10)
	if id := BootID(); id != "" {
		p.Start += ":" + id
	}
	if exe, err := os.Readlink(fmt.Sprintf("/proc/%d/exe", pid)); err == nil {
		p.Exe = strings.TrimSuffix(exe, " (deleted)")
	}
	p.UID = -1
	if fi, err := os.Stat(fmt.Sprintf("/proc/%d", pid)); err == nil {
		if st, ok := fi.Sys().(*syscall.Stat_t); ok {
			p.UID = int(st.Uid)
		}
	}
	return p, true
}

// Pids of every process.
func Pids() []int {
	ents, _ := os.ReadDir("/proc")
	var out []int
	for _, e := range ents {
		if n, err := strconv.Atoi(e.Name()); err == nil {
			out = append(out, n)
		}
	}
	return out
}

// UnitOf is the systemd unit of a process (from /proc/<pid>/cgroup), "" when none.
func UnitOf(pid int) string {
	b, err := os.ReadFile(fmt.Sprintf("/proc/%d/cgroup", pid))
	if err != nil {
		return ""
	}
	for _, ln := range strings.Split(string(b), "\n") {
		for _, part := range strings.Split(ln, "/") {
			if strings.HasSuffix(part, ".service") {
				return part
			}
		}
	}
	return ""
}

// UserName of a uid ("" when unknown).
func UserName(uid int) string {
	if uid < 0 {
		return ""
	}
	if u, err := user.LookupId(strconv.Itoa(uid)); err == nil {
		return u.Username
	}
	return strconv.Itoa(uid)
}

// OpenFiles is the set of paths held open by any process (readlink of /proc/*/fd/*), used to skip files in use.
func OpenFiles() map[string]bool {
	out := map[string]bool{}
	fds, _ := filepath.Glob("/proc/[0-9]*/fd/*")
	for _, fd := range fds {
		if t, err := os.Readlink(fd); err == nil && strings.HasPrefix(t, "/") {
			out[t] = true
		}
	}
	return out
}

// ---------------------------------------------------------------- file systems

// FSUsage of the file system holding p: size, available to unprivileged users, used percentage (as df computes it).
type FSUsage struct {
	Size, Avail, Used uint64
	UsePct            float64
	Mount, FSType     string
}

// Statfs measures the file system of a real path.
func Statfs(p string) (FSUsage, error) {
	var st syscall.Statfs_t
	if err := syscall.Statfs(p, &st); err != nil {
		return FSUsage{}, err
	}
	bs := uint64(st.Bsize)
	u := FSUsage{Size: st.Blocks * bs, Avail: st.Bavail * bs, Used: (st.Blocks - st.Bfree) * bs}
	if u.Used+u.Avail > 0 {
		u.UsePct = float64(u.Used) * 100 / float64(u.Used+u.Avail)
	}
	u.Mount = mountOf(p)
	return u, nil
}

// mountOf walks up while the device stays the same.
func mountOf(p string) string {
	fi, err := os.Stat(p)
	if err != nil {
		return "/"
	}
	dev := fi.Sys().(*syscall.Stat_t).Dev
	cur := filepath.Clean(p)
	for cur != "/" {
		parent := filepath.Dir(cur)
		pf, err := os.Stat(parent)
		if err != nil || pf.Sys().(*syscall.Stat_t).Dev != dev {
			return cur
		}
		cur = parent
	}
	return "/"
}

// ---------------------------------------------------------------- systemd units

// Units answers unit questions: systemctl on a real host, <sandbox>/.sim/units.json in simulator mode.
type Units struct {
	Env *Env
	mu  sync.Mutex
}

func (u *Units) simFile() string { return filepath.Join(u.Env.Sim, ".sim", "units.json") }

func (u *Units) simLoad() map[string]map[string]any {
	out := map[string]map[string]any{}
	b, err := os.ReadFile(u.simFile())
	if err == nil {
		_ = Decode(b, &out)
	}
	return out
}

func (u *Units) simSave(m map[string]map[string]any) error {
	b, _ := json.MarshalIndent(m, "", "  ")
	if err := os.MkdirAll(filepath.Dir(u.simFile()), 0o755); err != nil {
		return err
	}
	return os.WriteFile(u.simFile(), b, 0o644)
}

// Show returns ActiveState, SubState, MainPID, NRestarts, LoadState of a unit.
func (u *Units) Show(unit string) map[string]string {
	if u.Env != nil && u.Env.Sim != "" {
		u.mu.Lock()
		defer u.mu.Unlock()
		m, ok := u.simLoad()[unit]
		if !ok {
			return map[string]string{"LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead", "MainPID": "0", "NRestarts": "0"}
		}
		out := map[string]string{"LoadState": "loaded"}
		for _, k := range []string{"ActiveState", "SubState", "MainPID", "NRestarts"} {
			s, _ := pyStr(m[k])
			out[k] = s
		}
		return out
	}
	cmd := exec.Command("/usr/bin/systemctl", "show", "-p", "LoadState,ActiveState,SubState,MainPID,NRestarts", "--", unit)
	cmd.Env = []string{"PATH=/usr/sbin:/usr/bin:/sbin:/bin", "LANG=C"}
	out, _ := runQuiet(cmd, 10*time.Second)
	return parseKV(string(out))
}

// SimRestart restarts a simulated unit (new MainPID, NRestarts + 1).
func (u *Units) SimRestart(unit string) error {
	u.mu.Lock()
	defer u.mu.Unlock()
	m := u.simLoad()
	cur, ok := m[unit]
	if !ok {
		return fmt.Errorf("Failed to restart %s: Unit %s not found.", unit, unit)
	}
	n, _ := numOpt(cur, "NRestarts")
	pid, _ := numOpt(cur, "MainPID")
	cur["NRestarts"] = n + 1
	cur["MainPID"] = pid + 7
	cur["ActiveState"], cur["SubState"] = "active", "running"
	m[unit] = cur
	return u.simSave(m)
}

func runQuiet(cmd *exec.Cmd, timeout time.Duration) ([]byte, error) {
	done := make(chan struct{})
	var out []byte
	var err error
	go func() { out, err = cmd.Output(); close(done) }()
	select {
	case <-done:
		return out, err
	case <-time.After(timeout):
		if cmd.Process != nil {
			_ = cmd.Process.Kill()
		}
		<-done
		return out, fmt.Errorf("timeout")
	}
}

func parseKV(s string) map[string]string {
	out := map[string]string{}
	sc := bufio.NewScanner(strings.NewReader(s))
	for sc.Scan() {
		k, v, ok := strings.Cut(strings.TrimSpace(sc.Text()), "=")
		if ok && k != "" {
			out[strings.TrimSpace(k)] = strings.TrimSpace(v)
		}
	}
	return out
}
