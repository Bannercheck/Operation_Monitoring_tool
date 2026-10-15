package main

import (
	"os"
	"path/filepath"
	"testing"
)

func cg(t *testing.T, files map[string]string) string {
	d := t.TempDir()
	for p, v := range files {
		_ = os.MkdirAll(filepath.Dir(filepath.Join(d, p)), 0o755)
		if err := os.WriteFile(filepath.Join(d, p), []byte(v+"\n"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return d
}

func TestContainerLimits(t *testing.T) {
	for _, c := range []struct {
		name  string
		files map[string]string
		cpus  int
		mem   int64
	}{
		{"v2 two cpus", map[string]string{"cpu.max": "200000 100000", "memory.max": "2147483648"}, 2, 2 << 30},
		{"v2 half cpu rounds up", map[string]string{"cpu.max": "50000 100000", "memory.max": "max"}, 1, 0},
		{"v2 no limit", map[string]string{"cpu.max": "max 100000"}, 0, 0},
		{"v1", map[string]string{"cpu/cpu.cfs_quota_us": "150000", "cpu/cpu.cfs_period_us": "100000", "memory/memory.limit_in_bytes": "1073741824"}, 2, 1 << 30},
		{"v1 unlimited", map[string]string{"cpu/cpu.cfs_quota_us": "-1", "cpu/cpu.cfs_period_us": "100000", "memory/memory.limit_in_bytes": "9223372036854771712"}, 0, 0},
		{"nothing", map[string]string{}, 0, 0},
	} {
		cpus, mem := containerLimits(cg(t, c.files))
		if cpus != c.cpus || mem != c.mem {
			t.Errorf("%s: got %d cpus %d bytes, want %d / %d", c.name, cpus, mem, c.cpus, c.mem)
		}
	}
}
