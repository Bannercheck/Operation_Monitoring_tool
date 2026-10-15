package main

import (
	"log"
	"os"
	"path/filepath"
	"runtime"
	"runtime/debug"
	"strconv"
	"strings"
)

// containerLimits reads the CPU and memory limits the container runs under (cgroup v2, then v1) below root ("/sys/fs/cgroup").
// cpus is the quota rounded up (0 = no limit), mem the byte limit (0 = no limit). Go 1.24 sizes GOMAXPROCS from the node's CPUs, not
// the pod's limit: on a 64-core OpenShift node with a 2-CPU limit that means 64 parse workers fighting over 2 CPUs and being throttled.
func containerLimits(root string) (cpus int, mem int64) {
	read := func(p string) string {
		b, err := os.ReadFile(filepath.Join(root, p))
		if err != nil {
			return ""
		}
		return strings.TrimSpace(string(b))
	}
	if f := strings.Fields(read("cpu.max")); len(f) == 2 && f[0] != "max" { // v2: "<quota> <period>" or "max <period>"
		q, e1 := strconv.ParseFloat(f[0], 64)
		p, e2 := strconv.ParseFloat(f[1], 64)
		if e1 == nil && e2 == nil && q > 0 && p > 0 {
			cpus = int((q + p - 1) / p)
		}
	} else if q, err := strconv.ParseFloat(read("cpu/cpu.cfs_quota_us"), 64); err == nil && q > 0 { // v1
		if p, err := strconv.ParseFloat(read("cpu/cpu.cfs_period_us"), 64); err == nil && p > 0 {
			cpus = int((q + p - 1) / p)
		}
	}
	for _, p := range []string{"memory.max", "memory/memory.limit_in_bytes"} {
		if v, err := strconv.ParseInt(read(p), 10, 64); err == nil && v > 0 && v < 1<<50 { // v1 reports a huge number for "no limit"
			mem = v
			break
		}
	}
	return
}

// applyContainerLimits sets GOMAXPROCS and the GC memory limit from the container's limits unless GOMAXPROCS / GOMEMLIMIT are set
// explicitly. The parse stage's default worker count follows GOMAXPROCS, so it follows the pod's CPU limit too.
func applyContainerLimits(root string) {
	cpus, mem := containerLimits(root)
	if os.Getenv("GOMAXPROCS") == "" && cpus > 0 && cpus < runtime.NumCPU() {
		runtime.GOMAXPROCS(cpus)
	}
	if os.Getenv("GOMEMLIMIT") == "" && mem > 0 {
		debug.SetMemoryLimit(mem / 10 * 9)
	}
	log.Printf("limits: GOMAXPROCS=%d (container cpus %d, node %d), memory limit %d MiB", runtime.GOMAXPROCS(0), cpus, runtime.NumCPU(), mem>>20)
}
