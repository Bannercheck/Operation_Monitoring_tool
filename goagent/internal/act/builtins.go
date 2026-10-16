package act

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"io"
	"io/fs"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// Built-in functions (§5.7): pipes are not allowed, so "the ten biggest directories" is one function, not du | sort | head.
// They return structured fields (numbers stay numbers; fractions travel as strings, the canonical form has no floats) or,
// for privacy: raw templates, display text.

var builtinKinds = map[string]string{"disk_usage": "diag", "dir_top": "diag", "files_largest": "diag", "compress_older_than": "act",
	"decompress_list": "act", "log_tail": "diag", "proc_top": "diag", "proc_find": "diag", "port_check": "diag", "http_check": "diag",
	"service_status": "diag", "win_service_status": "diag", "iis_pool_state": "diag", "sap_wp_table": "diag"}

const walkMax = 200000

// BuiltinCall is what a built-in gets.
type BuiltinCall struct {
	Ctx   context.Context
	A     *Actuator
	Job   *JobRun
	Vals  map[string]any
	Dry   bool
	Tmpl  *Template
	Start time.Time
}

// BuiltinOut is what a built-in returns.
type BuiltinOut struct {
	Fields  map[string]any
	Display string
	Reject  string // a reject code decided inside the built-in (manifest_failed)
}

type builtinFn func(c *BuiltinCall) (BuiltinOut, error)

var builtins = map[string]builtinFn{
	"disk_usage": biDiskUsage, "dir_top": biDirTop, "files_largest": biFilesLargest, "compress_older_than": biCompress,
	"decompress_list": biDecompress, "log_tail": biLogTail, "proc_top": biProcTop, "proc_find": biProcFind, "port_check": biPortCheck,
	"http_check": biHTTPCheck, "service_status": biServiceStatus,
}

func str(v any) string { s, _ := pyStr(v); return s }

func intVal(v any, def int) int {
	if n, err := strconv.Atoi(str(v)); err == nil {
		return n
	}
	return def
}

// frac renders a fraction for the canonical form ("93.4").
func frac(f float64) string { return strconv.FormatFloat(f, 'f', 1, 64) }

// DiskUsage of a logical path. Simulator: <sandbox>/.sim/disk.json {"size_bytes", "base_used_bytes"} makes a small virtual
// disk whose use follows the files under the sandbox, so compressing logs really frees "disk".
func DiskUsage(env *Env, logical string) (FSUsage, error) {
	if env != nil && env.Sim != "" {
		b, err := os.ReadFile(filepath.Join(env.Sim, ".sim", "disk.json"))
		if err == nil {
			var d map[string]any
			if Decode(b, &d) == nil {
				size, _ := numOpt(d, "size_bytes")
				base, _ := numOpt(d, "base_used_bytes")
				var used int64
				_ = filepath.WalkDir(env.Sim, func(p string, de fs.DirEntry, err error) error {
					if err != nil {
						return nil
					}
					if de.IsDir() && de.Name() == ".sim" {
						return filepath.SkipDir
					}
					if de.Type().IsRegular() {
						if fi, err := de.Info(); err == nil {
							used += fi.Size()
						}
					}
					return nil
				})
				used += base
				if size <= 0 {
					size = 1
				}
				avail := size - used
				if avail < 0 {
					avail = 0
				}
				return FSUsage{Size: uint64(size), Avail: uint64(avail), Used: uint64(used), UsePct: float64(used) * 100 / float64(size), Mount: "/", FSType: "simfs"}, nil
			}
		}
		u, err := Statfs(env.Real(logical))
		if err == nil {
			u.Mount = env.Logical(u.Mount)
			if u.Mount == "" {
				u.Mount = "/"
			}
		}
		return u, err
	}
	return Statfs(logical)
}

func biDiskUsage(c *BuiltinCall) (BuiltinOut, error) {
	p := str(c.Vals["path"])
	u, err := DiskUsage(c.A.Env, p)
	if err != nil {
		return BuiltinOut{}, err
	}
	gb := func(b uint64) string { return frac(float64(b) / (1 << 30)) }
	f := map[string]any{"use_pct": frac(u.UsePct), "avail_gb": gb(u.Avail), "size_gb": gb(u.Size), "mount": u.Mount}
	if u.FSType != "" {
		f["fs"] = u.FSType
	}
	return BuiltinOut{Fields: f}, nil
}

// walk visits regular files under a real root on one file system, bounded in entries and by the context.
func walk(ctx context.Context, root string, fn func(p string, fi fs.FileInfo, depth int)) error {
	rfi, err := os.Stat(root)
	if err != nil {
		return err
	}
	dev := rfi.Sys().(*syscall.Stat_t).Dev
	n := 0
	return filepath.WalkDir(root, func(p string, de fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		n++
		if n > walkMax {
			return errors.New("too many entries (200 000)")
		}
		if n%1000 == 0 && ctx.Err() != nil {
			return ctx.Err()
		}
		fi, err := de.Info()
		if err != nil {
			return nil
		}
		if de.IsDir() {
			if st, ok := fi.Sys().(*syscall.Stat_t); ok && st.Dev != dev {
				return filepath.SkipDir
			}
			if de.Name() == ".sim" && p == filepath.Join(root, ".sim") {
				return filepath.SkipDir
			}
			return nil
		}
		if fi.Mode().IsRegular() {
			rel, _ := filepath.Rel(root, p)
			fn(p, fi, strings.Count(rel, string(filepath.Separator)))
		}
		return nil
	})
}

func biDirTop(c *BuiltinCall) (BuiltinOut, error) {
	logical := str(c.Vals["path"])
	root := c.A.Env.Real(logical)
	depth, n := intVal(c.Vals["depth"], 1), intVal(c.Vals["n"], 10)
	sizes := map[string]int64{}
	err := walk(c.Ctx, root, func(p string, fi fs.FileInfo, d int) {
		rel, _ := filepath.Rel(root, p)
		parts := strings.Split(rel, string(filepath.Separator))
		k := depth
		if len(parts)-1 < k {
			k = len(parts) - 1
		}
		dir := filepath.Join(append([]string{logical}, parts[:k]...)...)
		sizes[dir] += fi.Size()
	})
	if err != nil {
		return BuiltinOut{}, err
	}
	type row struct {
		p string
		b int64
	}
	var rows []row
	for p, b := range sizes {
		rows = append(rows, row{p, b})
	}
	sort.Slice(rows, func(i, j int) bool { return rows[i].b > rows[j].b || rows[i].b == rows[j].b && rows[i].p < rows[j].p })
	if len(rows) > n {
		rows = rows[:n]
	}
	out := []any{}
	for _, r := range rows {
		out = append(out, map[string]any{"path": r.p, "bytes": r.b})
	}
	return BuiltinOut{Fields: map[string]any{"rows": out}}, nil
}

func biFilesLargest(c *BuiltinCall) (BuiltinOut, error) {
	logical := str(c.Vals["path"])
	root := c.A.Env.Real(logical)
	n := intVal(c.Vals["n"], 10)
	type row struct {
		p     string
		b, mt int64
	}
	var rows []row
	err := walk(c.Ctx, root, func(p string, fi fs.FileInfo, d int) {
		rows = append(rows, row{c.A.Env.Logical(p), fi.Size(), fi.ModTime().Unix()})
	})
	if err != nil {
		return BuiltinOut{}, err
	}
	sort.Slice(rows, func(i, j int) bool { return rows[i].b > rows[j].b || rows[i].b == rows[j].b && rows[i].p < rows[j].p })
	if len(rows) > n {
		rows = rows[:n]
	}
	out := []any{}
	for _, r := range rows {
		out = append(out, map[string]any{"path": r.p, "bytes": r.b, "mtime": r.mt})
	}
	return BuiltinOut{Fields: map[string]any{"rows": out}}, nil
}

func biLogTail(c *BuiltinCall) (BuiltinOut, error) {
	p := c.A.Env.Real(str(c.Vals["path"]))
	n := intVal(c.Vals["n"], 50)
	f, err := os.Open(p)
	if err != nil {
		return BuiltinOut{}, err
	}
	defer f.Close()
	fi, _ := f.Stat()
	const window = 256 * 1024
	off := fi.Size() - window
	if off < 0 {
		off = 0
	}
	_, _ = f.Seek(off, io.SeekStart)
	b, _ := io.ReadAll(io.LimitReader(f, window))
	lines := strings.Split(strings.TrimRight(string(b), "\n"), "\n")
	if off > 0 && len(lines) > 0 {
		lines = lines[1:]
	}
	if len(lines) > n {
		lines = lines[len(lines)-n:]
	}
	return BuiltinOut{Display: strings.Join(lines, "\n")}, nil
}

// owner shown for a process: service accounts (uid < 1000) by name, people pseudonymised on the host (§5.7).
func (a *Actuator) owner(uid int) string {
	name := UserName(uid)
	if uid >= 0 && uid < 1000 {
		return name
	}
	return a.Pseudonym(name)
}

func biProcTop(c *BuiltinCall) (BuiltinOut, error) {
	n := intVal(c.Vals["n"], 10)
	first := map[int]ProcInfo{}
	for _, pid := range Pids() {
		if pi, ok := ReadProc(pid); ok {
			first[pid] = pi
		}
	}
	t0 := time.Now()
	select {
	case <-time.After(500 * time.Millisecond):
	case <-c.Ctx.Done():
		return BuiltinOut{}, c.Ctx.Err()
	}
	secs := time.Since(t0).Seconds()
	type row struct {
		pi  ProcInfo
		cpu float64
	}
	var rows []row
	for pid, a := range first {
		b, ok := ReadProc(pid)
		if !ok || b.Start != a.Start {
			continue
		}
		rows = append(rows, row{b, float64(b.CPU-a.CPU) / 100.0 / secs * 100})
	}
	sort.Slice(rows, func(i, j int) bool {
		return rows[i].cpu > rows[j].cpu || rows[i].cpu == rows[j].cpu && rows[i].pi.PID < rows[j].pi.PID
	})
	if len(rows) > n {
		rows = rows[:n]
	}
	out := []any{}
	for _, r := range rows {
		out = append(out, map[string]any{"pid": r.pi.PID, "start": r.pi.Start, "comm": r.pi.Comm, "user": c.A.owner(r.pi.UID),
			"cpu": frac(r.cpu), "mem_mb": r.pi.RSSKB / 1024})
	}
	return BuiltinOut{Fields: map[string]any{"rows": out}}, nil
}

func biProcFind(c *BuiltinCall) (BuiltinOut, error) {
	pat := str(c.Vals["pattern"])
	out := []any{}
	for _, pid := range Pids() {
		pi, ok := ReadProc(pid)
		if !ok || (pi.Comm != pat && filepath.Base(pi.Exe) != pat) {
			continue
		}
		out = append(out, map[string]any{"pid": pi.PID, "start": pi.Start, "exe": pi.Exe, "unit": UnitOf(pid), "user": c.A.owner(pi.UID)})
		if len(out) >= 50 {
			break
		}
	}
	return BuiltinOut{Fields: map[string]any{"rows": out}}, nil
}

func biPortCheck(c *BuiltinCall) (BuiltinOut, error) {
	port := intVal(c.Vals["port"], 0)
	d := net.Dialer{Timeout: 5 * time.Second}
	conn, err := d.DialContext(c.Ctx, "tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(port)))
	open := "no"
	if err == nil {
		open = "yes"
		conn.Close()
	}
	return BuiltinOut{Fields: map[string]any{"port": port, "open": open}}, nil
}

func biHTTPCheck(c *BuiltinCall) (BuiltinOut, error) {
	u := str(c.Vals["url"])
	ctx, cancel := context.WithTimeout(c.Ctx, 5*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return BuiltinOut{}, err
	}
	cl := &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	t0 := time.Now()
	resp, err := cl.Do(req)
	ms := time.Since(t0).Milliseconds()
	if err != nil {
		return BuiltinOut{Fields: map[string]any{"status": 0, "ms": ms}}, nil
	}
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 64*1024))
	resp.Body.Close()
	return BuiltinOut{Fields: map[string]any{"status": resp.StatusCode, "ms": ms}}, nil
}

func biServiceStatus(c *BuiltinCall) (BuiltinOut, error) {
	st := c.A.Units.Show(str(c.Vals["unit"]))
	f := map[string]any{"ActiveState": st["ActiveState"], "SubState": st["SubState"]}
	for _, k := range []string{"MainPID", "NRestarts"} {
		if n, err := strconv.Atoi(st[k]); err == nil {
			f[k] = n
		}
	}
	return BuiltinOut{Fields: f}, nil
}

// --------------------------------------------------------------- parsers for argv / script output (§9.1 names)

var reNum = func(s string) (int64, bool) {
	n, err := strconv.ParseInt(strings.TrimSpace(s), 10, 64)
	return n, err == nil
}

// ParseOutput turns the stdout of an argv / script template into fields with its named parser (nil: no parser for it).
func ParseOutput(parser, out string) map[string]any {
	switch parser {
	case "systemctl_show":
		kv := parseKV(out)
		f := map[string]any{}
		for _, k := range []string{"ActiveState", "SubState"} {
			if v, ok := kv[k]; ok {
				f[k] = v
			}
		}
		for _, k := range []string{"MainPID", "NRestarts"} {
			if n, ok := reNum(kv[k]); ok {
				f[k] = n
			}
		}
		return f
	case "kv", "compress_result", "http_status", "port_state":
		var m map[string]any
		if json.Unmarshal([]byte(out), &m) == nil {
			return m
		}
		f := map[string]any{}
		for k, v := range parseKV(out) {
			if n, ok := reNum(v); ok {
				f[k] = n
			} else {
				f[k] = v
			}
		}
		return f
	case "lsof_deleted":
		f := map[string]any{"rows": []any{}}
		rows := []any{}
		sc := bufio.NewScanner(strings.NewReader(out))
		for sc.Scan() {
			ln := strings.TrimSpace(sc.Text())
			if strings.HasPrefix(ln, "deleted_open_bytes=") {
				for _, part := range strings.Fields(ln) {
					k, v, _ := strings.Cut(part, "=")
					if n, ok := reNum(v); ok {
						f[k] = n
					}
				}
				continue
			}
			parts := strings.SplitN(ln, " ", 4)
			if len(parts) == 4 && len(rows) < 50 {
				pid, _ := reNum(parts[0])
				size, _ := reNum(parts[2])
				rows = append(rows, map[string]any{"pid": pid, "comm": parts[1], "bytes": size, "path": parts[3]})
			}
		}
		f["rows"] = rows
		return f
	case "jcmd_heap":
		f := map[string]any{}
		for _, ln := range strings.Split(out, "\n") {
			fs := strings.Fields(strings.NewReplacer(",", " ").Replace(ln))
			for i := 0; i+1 < len(fs); i++ {
				v := strings.TrimSuffix(fs[i+1], "K")
				n, ok := reNum(v)
				if !ok {
					continue
				}
				switch {
				case fs[i] == "used" && f["used_kb"] == nil:
					f["used_kb"] = n
				case fs[i] == "total" && f["capacity_kb"] == nil:
					f["capacity_kb"] = n
				}
			}
			if strings.Contains(ln, "Metaspace") {
				for i := 0; i+1 < len(fs); i++ {
					if fs[i] == "used" {
						if n, ok := reNum(strings.TrimSuffix(fs[i+1], "K")); ok {
							f["metaspace_kb"] = n
						}
					}
				}
			}
		}
		return f
	case "journal_vacuum":
		var freed int64
		for _, ln := range strings.Split(out, "\n") {
			if i := strings.Index(ln, "freed "); i >= 0 {
				freed += sizeBytes(strings.Fields(ln[i+6:])[0])
			}
		}
		return map[string]any{"freed_bytes": freed}
	case "logrotate_result":
		rot, errs := 0, 0
		for _, ln := range strings.Split(out, "\n") {
			l := strings.ToLower(ln)
			if strings.Contains(l, "rotating log") || strings.Contains(l, "renaming") {
				rot++
			}
			if strings.HasPrefix(l, "error") {
				errs++
			}
		}
		return map[string]any{"rotated": rot, "errors": errs}
	}
	return nil
}

func sizeBytes(s string) int64 {
	s = strings.TrimSpace(strings.TrimSuffix(s, "B"))
	mult := int64(1)
	switch {
	case strings.HasSuffix(s, "K"):
		mult, s = 1024, strings.TrimSuffix(s, "K")
	case strings.HasSuffix(s, "M"):
		mult, s = 1024*1024, strings.TrimSuffix(s, "M")
	case strings.HasSuffix(s, "G"):
		mult, s = 1024*1024*1024, strings.TrimSuffix(s, "G")
	}
	f, err := strconv.ParseFloat(s, 64)
	if err != nil {
		return 0
	}
	return int64(f * float64(mult))
}
