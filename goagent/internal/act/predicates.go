package act

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

// Agent-side predicates (§5.6, closed set): a failing predicate answers rejected:precondition and nothing runs.
var predicateArity = map[string]int{"path_under": 2, "fs_free_pct_below": 2, "fs_free_pct_above": 2, "proc_comm_is": 2, "proc_user_is": 2,
	"proc_age_min": 2, "unit_exists": 1, "unit_state_is": 2, "win_service_exists": 1, "iis_pool_exists": 1, "file_age_min": 2, "host_is": 1}

type predEnv struct {
	env   *Env
	units *Units
	host  string
	vals  map[string]any
	now   time.Time
}

// argOf renders a predicate argument: placeholders are filled with the validated parameter values.
func (p *predEnv) arg(a any) (string, any, error) {
	if s, ok := a.(string); ok {
		r, err := Fill(s, p.vals)
		if err != nil {
			return "", nil, err
		}
		// a whole-element placeholder keeps the structured value (pid pairs)
		if ph := placeholders(s); len(ph) == 1 && s == "{"+ph[0]+"}" {
			return r, p.vals[ph[0]], nil
		}
		return r, r, nil
	}
	s, _ := pyStr(a)
	return s, a, nil
}

func pidOf(v any) (int, string) {
	if m, ok := v.(map[string]any); ok {
		ps, _ := pyStr(m["pid"])
		pid, _ := strconv.Atoi(ps)
		st, _ := pyStr(m["start"])
		return pid, st
	}
	s, _ := pyStr(v)
	pid, _ := strconv.Atoi(s)
	return pid, ""
}

// evalPredicate: ok, or the reason it failed (shown to people, no secrets).
func (p *predEnv) eval(name string, args []any) (bool, string) {
	if want, ok := predicateArity[name]; !ok || len(args) != want {
		return false, "unknown predicate " + name
	}
	a0, v0, err := p.arg(args[0])
	if err != nil {
		return false, err.Error()
	}
	a1 := ""
	if len(args) > 1 {
		if a1, _, err = p.arg(args[1]); err != nil {
			return false, err.Error()
		}
	}
	switch name {
	case "path_under":
		pre := a1
		if a0 == strings.TrimRight(pre, "/") || strings.HasPrefix(a0, strings.TrimRight(pre, "/")+"/") {
			return true, ""
		}
		return false, fmt.Sprintf("%s is not under %s", a0, pre)
	case "fs_free_pct_below", "fs_free_pct_above":
		lim, err := strconv.ParseFloat(a1, 64)
		if err != nil {
			return false, "bad percentage"
		}
		u, err := DiskUsage(p.env, a0)
		if err != nil {
			return false, err.Error()
		}
		free := 100 - u.UsePct
		if name == "fs_free_pct_below" && free < lim || name == "fs_free_pct_above" && free > lim {
			return true, ""
		}
		return false, fmt.Sprintf("free space on %s is %.1f%%", u.Mount, free)
	case "proc_comm_is", "proc_user_is", "proc_age_min":
		pid, start := pidOf(v0)
		pi, ok := ReadProc(pid)
		if !ok || (start != "" && pi.Start != start) {
			return false, "process gone or recycled"
		}
		switch name {
		case "proc_comm_is":
			return pi.Comm == a1, "process name is " + pi.Comm
		case "proc_user_is":
			return UserName(pi.UID) == a1, "process owner differs"
		default:
			secs, _ := strconv.ParseFloat(a1, 64)
			age := processAge(pi)
			return age >= secs, fmt.Sprintf("process age %.0f s", age)
		}
	case "unit_exists":
		st := p.units.Show(a0)
		return st["LoadState"] == "loaded", "unit " + a0 + " not found"
	case "unit_state_is":
		st := p.units.Show(a0)
		return st["ActiveState"] == a1, "unit is " + st["ActiveState"]
	case "win_service_exists", "iis_pool_exists":
		return false, "a Windows predicate on a Linux host"
	case "file_age_min":
		fi, err := os.Stat(p.env.Real(a0))
		if err != nil {
			return false, a0 + " not found"
		}
		secs, _ := strconv.ParseFloat(a1, 64)
		return p.now.Sub(fi.ModTime()).Seconds() >= secs, "file is newer"
	case "host_is":
		return strings.EqualFold(a0, p.host), "this host is " + p.host
	}
	return false, "unknown predicate " + name
}

func processAge(pi ProcInfo) float64 {
	b, err := os.ReadFile("/proc/uptime")
	if err != nil {
		return 0
	}
	up, _ := strconv.ParseFloat(strings.Fields(string(b))[0], 64)
	return up - float64(pi.Ticks)/100.0
}
