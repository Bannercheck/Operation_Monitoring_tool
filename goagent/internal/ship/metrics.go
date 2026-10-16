package ship

import (
	"bufio"
	"bytes"
	"context"
	"os"
	"os/exec"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// Host metrics exactly as agent.py's host_metrics(): cpu = 1-minute load average per online CPU in percent (capped at 100),
// memory = 1 - MemAvailable/MemTotal, disk = used / total of "/", gpu = the busiest NVIDIA GPU (nvidia-smi, when present);
// every value a percentage rounded to one decimal. Names and units are what the dashboard's metric charts expect.

var procRoot = getenv("WATCHOVER_PROC", "/proc") // tests point it at a fake /proc

func onlineCPUs() int {
	b, err := os.ReadFile("/sys/devices/system/cpu/online") // what os.cpu_count() (sysconf _SC_NPROCESSORS_ONLN) counts
	if err == nil {
		n := 0
		for _, part := range strings.Split(strings.TrimSpace(string(b)), ",") {
			lo, hi, found := strings.Cut(part, "-")
			a, err1 := strconv.Atoi(lo)
			if !found {
				if err1 == nil {
					n++
				}
				continue
			}
			z, err2 := strconv.Atoi(hi)
			if err1 == nil && err2 == nil && z >= a {
				n += z - a + 1
			}
		}
		if n > 0 {
			return n
		}
	}
	return runtime.NumCPU()
}

func round1(x float64) r1 {
	v, _ := strconv.ParseFloat(strconv.FormatFloat(x, 'f', 1, 64), 64)
	return r1(v)
}

type metricsProbe struct {
	ncpu      int
	nvsmi     string
	nvChecked time.Time
}

func (m *metricsProbe) sample(host, env string, now time.Time) obj {
	o := obj{{"ts", isoUTC(now)}, {"host", host}}
	if b, err := os.ReadFile(procRoot + "/loadavg"); err == nil {
		if f := strings.Fields(string(b)); len(f) > 0 {
			if l1, err := strconv.ParseFloat(f[0], 64); err == nil {
				if m.ncpu == 0 {
					m.ncpu = onlineCPUs()
				}
				o = append(o, kv{"cpu", round1(minf(100, 100*l1/float64(max(m.ncpu, 1))))})
			}
		}
	}
	if total, avail, ok := meminfo(); ok && total > 0 {
		o = append(o, kv{"memory", round1(100 * (1 - avail/total))})
	}
	var st syscall.Statfs_t
	if syscall.Statfs("/", &st) == nil && st.Blocks > 0 {
		total := float64(st.Blocks) * float64(st.Frsize)
		used := float64(st.Blocks-st.Bfree) * float64(st.Frsize)
		o = append(o, kv{"disk", round1(100 * used / total)})
	}
	if g, ok := m.gpu(); ok {
		o = append(o, kv{"gpu", g})
	}
	if env != "" {
		o = append(o, kv{"env", env})
	}
	return o
}

func minf(a, b float64) float64 {
	if a < b {
		return a
	}
	return b
}

func meminfo() (total, avail float64, ok bool) {
	f, err := os.Open(procRoot + "/meminfo")
	if err != nil {
		return 0, 0, false
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	var t, a = -1.0, -1.0
	for sc.Scan() && (t < 0 || a < 0) {
		k, v, found := strings.Cut(sc.Text(), ":")
		if !found {
			continue
		}
		fs := strings.Fields(v)
		if len(fs) == 0 {
			continue
		}
		n, err := strconv.ParseFloat(fs[0], 64)
		if err != nil {
			continue
		}
		switch k {
		case "MemTotal":
			t = n
		case "MemAvailable":
			a = n
		}
	}
	return t, a, t >= 0 && a >= 0
}

// gpu runs nvidia-smi only where it exists (looked up again every 10 minutes): no process per sample on other hosts.
func (m *metricsProbe) gpu() (r1, bool) {
	if time.Since(m.nvChecked) > 10*time.Minute {
		m.nvsmi, _ = exec.LookPath("nvidia-smi")
		m.nvChecked = time.Now()
	}
	if m.nvsmi == "" {
		return 0, false
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, m.nvsmi, "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits").Output()
	if err != nil && len(out) == 0 {
		return 0, false
	}
	best, any := 0.0, false
	for _, x := range bytes.Fields(out) {
		s := strings.TrimSpace(string(x))
		d := strings.ReplaceAll(s, ".", "")
		if d == "" || strings.Trim(d, "0123456789") != "" {
			continue
		}
		v, err := strconv.ParseFloat(s, 64)
		if err != nil {
			continue
		}
		if !any || v > best {
			best, any = v, true
		}
	}
	return round1(best), any
}

// metricsLine is one metrics.jsonl body.
func (m *metricsProbe) line(host, env string, now time.Time) []byte {
	return append(m.sample(host, env, now).bytes(true), '\n')
}
