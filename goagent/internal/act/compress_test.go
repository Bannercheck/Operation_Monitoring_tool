package act

import (
	"context"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"syscall"
	"testing"
	"time"
)

func writeOld(t *testing.T, p string, size int, age time.Duration) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte(strings.Repeat("log line 0123456789\n", size/20+1)[:size]), 0o640); err != nil {
		t.Fatal(err)
	}
	old := time.Now().Add(-age)
	_ = os.Chtimes(p, old, old)
}

func listTree(t *testing.T, root string) []string {
	var out []string
	_ = filepath.Walk(root, func(p string, fi os.FileInfo, err error) error {
		if err == nil && fi.Mode().IsRegular() {
			rel, _ := filepath.Rel(root, p)
			out = append(out, rel)
		}
		return nil
	})
	sort.Strings(out)
	return out
}

func compressRig(t *testing.T) *rig {
	r := newRig(t, "R1", nil)
	logs := filepath.Join(r.sim, "var", "log", "app")
	writeOld(t, filepath.Join(logs, "orders.log.1"), 400000, 10*24*time.Hour)
	writeOld(t, filepath.Join(logs, "orders.log.2"), 300000, 12*24*time.Hour)
	writeOld(t, filepath.Join(logs, "orders.log"), 100000, 10*24*time.Hour)    // live log: never touched
	writeOld(t, filepath.Join(logs, "orders.log.3"), 200000, 2*24*time.Hour)   // too young
	writeOld(t, filepath.Join(logs, "orders.log.4.gz"), 1000, 20*24*time.Hour) // already compressed
	// the virtual disk is nearly full so the fs_free_pct_below(path, 20) predicate holds
	_ = os.WriteFile(filepath.Join(r.sim, ".sim", "disk.json"), []byte(`{"size_bytes": 1100000, "base_used_bytes": 0}`), 0o644)
	r.cycle(t)
	return r
}

func TestCompressManifestFirstAndRollback(t *testing.T) {
	r := compressRig(t)
	logs := filepath.Join(r.sim, "var", "log", "app")
	before := listTree(t, logs)
	tm := r.tmpl("linux.logs.compress_old")
	manifestSeen := false
	beforeFirstChange = func() {
		m := filepath.Join(r.state, "compress-J-test-0002.json")
		if _, err := os.Stat(m); err == nil {
			manifestSeen = true
		}
		if _, err := os.Stat(filepath.Join(logs, "orders.log.1")); err != nil {
			t.Error("a file changed before the manifest was durable")
		}
	}
	defer func() { beforeFirstChange = func() {} }()
	dry := r.f.job(tm, "dry_run", map[string]any{"path": "/var/log", "days": 7}, nil)
	act := r.f.job(tm, "execute", map[string]any{"path": "/var/log", "days": 7}, nil)
	r.run(t, dry)
	if f := r.f.result("J-test-0001")["fields"].(map[string]any); str(f["files"]) != "2" || str(f["bytes_before"]) != "700000" {
		t.Fatalf("dry run %v", f)
	}
	if got := listTree(t, logs); strings.Join(got, ",") != strings.Join(before, ",") {
		t.Fatalf("the dry run changed files: %v", got)
	}
	r.run(t, act)
	res := r.f.result("J-test-0002")
	if res["status"] != "done" || !manifestSeen {
		t.Fatalf("compress %v %v manifest %v", res["status"], res["stderr_tail"], manifestSeen)
	}
	f := res["fields"].(map[string]any)
	if str(f["files"]) != "2" || f["list_id"] != "J-test-0002" {
		t.Fatalf("fields %v", f)
	}
	for _, n := range []string{"orders.log.1.gz", "orders.log.2.gz", "orders.log", "orders.log.3", "orders.log.4.gz"} {
		if _, err := os.Stat(filepath.Join(logs, n)); err != nil {
			t.Errorf("%s missing after compress", n)
		}
	}
	fi, _ := os.Stat(filepath.Join(logs, "orders.log.1.gz"))
	if time.Since(fi.ModTime()) < 9*24*time.Hour || fi.Mode().Perm() != 0o640 {
		t.Errorf("mtime / mode not kept: %v %v", fi.ModTime(), fi.Mode())
	}
	// rollback from the manifest restores exactly the job's files
	rb := r.tmpl("linux.logs.decompress_list")
	r.run(t, r.f.job(rb, "rollback", map[string]any{"list_id": "J-test-0002"}, nil))
	if res := r.f.result("J-test-0003"); res["status"] != "done" || str(res["fields"].(map[string]any)["files"]) != "2" {
		t.Fatalf("rollback %v", res)
	}
	if got := listTree(t, logs); strings.Join(got, ",") != strings.Join(before, ",") {
		t.Fatalf("after rollback %v, before %v", got, before)
	}
}

func TestCompressCtimeFallbackRestoresOnlyTheJobsFiles(t *testing.T) {
	r := compressRig(t)
	logs := filepath.Join(r.sim, "var", "log", "app")
	time.Sleep(1500 * time.Millisecond) // ctime of the pre-existing .gz is well before the job's start (more than ctimeSlack)
	// a ctime that reads earlier than the job's start: RHEL 9's kernel stamps it from its coarse clock (a tick behind the precise
	// one the start comes from), a file system with 1 s timestamps truncates it. A start half a second "late" is that case
	// (the image build failed on a RHEL 9.8 VM with files:0)
	r.a.Cfg.Now = func() time.Time { return time.Now().Add(500 * time.Millisecond) }
	r.run(t, r.f.job(r.tmpl("linux.logs.compress_old"), "execute", map[string]any{"path": "/var/log", "days": 7}, nil))
	if r.f.result("J-test-0001")["status"] != "done" {
		t.Fatal("compress failed")
	}
	_ = os.Remove(filepath.Join(r.state, "compress-J-test-0001.json")) // the manifest is lost
	r.run(t, r.f.job(r.tmpl("linux.logs.decompress_list"), "rollback", map[string]any{"list_id": "J-test-0001"}, nil))
	f := r.f.result("J-test-0002")["fields"].(map[string]any)
	if str(f["files"]) != "2" || f["fallback"] != "ctime" {
		t.Fatalf("fallback %v", f)
	}
	if _, err := os.Stat(filepath.Join(logs, "orders.log.4.gz")); err != nil {
		t.Fatal("a .gz the job did not create was restored")
	}
}

func TestCompressFullDiskReleasesReserveOrRefuses(t *testing.T) {
	r := compressRig(t)
	logs := filepath.Join(r.sim, "var", "log", "app")
	before := listTree(t, logs)
	calls := 0
	writeDurable = func(p string, b []byte) error {
		if strings.Contains(p, "compress-") {
			calls++
			if calls == 1 {
				return syscall.ENOSPC
			}
		}
		return writeFileDurable(p, b)
	}
	defer func() { writeDurable = writeFileDurable }()
	r.run(t, r.f.job(r.tmpl("linux.logs.compress_old"), "dry_run", map[string]any{"path": "/var/log", "days": 7}, nil))
	r.run(t, r.f.job(r.tmpl("linux.logs.compress_old"), "execute", map[string]any{"path": "/var/log", "days": 7}, nil))
	if res := r.f.result("J-test-0002"); res["status"] != "done" || calls != 2 {
		t.Fatalf("reserve path: %v calls %d", res["status"], calls)
	}
	// always full: refused with manifest_failed and nothing changed
	r.run(t, r.f.job(r.tmpl("linux.logs.decompress_list"), "rollback", map[string]any{"list_id": "J-test-0002"}, nil))
	writeDurable = func(p string, b []byte) error {
		if strings.Contains(p, "compress-") {
			return syscall.ENOSPC
		}
		return writeFileDurable(p, b)
	}
	r.run(t, r.f.job(r.tmpl("linux.logs.compress_old"), "execute", map[string]any{"path": "/var/log", "days": 7}, nil))
	if res := r.f.result("J-test-0004"); res["status"] != "rejected" || res["reject_code"] != "manifest_failed" {
		t.Fatalf("full disk: %v %v", res["status"], res["reject_code"])
	}
	if got := listTree(t, logs); strings.Join(got, ",") != strings.Join(before, ",") {
		t.Fatalf("files changed although the manifest failed: %v", got)
	}
}

func TestArgvIsNeverAShell(t *testing.T) {
	res, err := RunArgv(context.Background(), []string{"/bin/echo", "a;b", "$(id)", "`id`", "| cat"}, nil, []string{"PATH=/usr/bin:/bin"}, 4096, 5*time.Second)
	if err != nil || res.ExitCode != 0 {
		t.Fatal(err, res.ExitCode)
	}
	if got := strings.TrimSpace(string(res.Stdout)); got != "a;b $(id) `id` | cat" {
		t.Fatalf("argv was interpreted: %q", got)
	}
	if _, err := RunArgv(context.Background(), []string{"/bin/echo", "a\nb"}, nil, nil, 4096, time.Second); err == nil {
		t.Fatal("a newline inside an element must be refused")
	}
}

func TestTimeoutKillsTheProcessGroup(t *testing.T) {
	killGrace = 500 * time.Millisecond
	defer func() { killGrace = 10 * time.Second }()
	marker := filepath.Join(t.TempDir(), "child.pid")
	// the child of the job forks a grandchild that would outlive a plain kill of the child
	res, _ := RunArgv(context.Background(), []string{"/bin/sh", "-c", "sleep 60 & echo $! > " + marker + "; wait"}, nil, nil, 4096, 700*time.Millisecond)
	if !res.TimedOut {
		t.Fatalf("not timed out: %+v", res)
	}
	b, err := os.ReadFile(marker)
	if err != nil {
		t.Fatal(err)
	}
	pid := strings.TrimSpace(string(b))
	time.Sleep(200 * time.Millisecond)
	if _, err := os.Stat("/proc/" + pid); err == nil {
		st, _ := os.ReadFile("/proc/" + pid + "/stat")
		if !strings.Contains(string(st), ") Z ") {
			t.Fatalf("grandchild %s survived the timeout", pid)
		}
	}
}

func TestOutputCapAndScrub(t *testing.T) {
	res, _ := RunArgv(context.Background(), []string{"/bin/sh", "-c", "yes x | head -c 10000; echo PGPASSWORD=Canary-Env-22 >&2"}, nil, nil, 1024, 5*time.Second)
	if len(res.Stdout) != 1024 || !res.Truncated {
		t.Fatalf("cap: %d %v", len(res.Stdout), res.Truncated)
	}
	if s := ScrubStrict(string(res.Stderr)); strings.Contains(s, "Canary") {
		t.Fatalf("stderr not scrubbed: %q", s)
	}
}
