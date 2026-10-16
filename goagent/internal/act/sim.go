package act

import (
	"fmt"
	"path/filepath"
	"strings"
)

// Simulator mode (`wo-agent act --sim <dir>`, the design's --actions-sim): every path of a template or job lives under the
// sandbox, built-ins run for real against it (a virtual disk in <dir>/.sim/disk.json makes compression free "disk"),
// argv and script templates are not executed — systemctl, logrotate, journalctl and jcmd answer from <dir>/.sim state —
// no account switching happens, and hello reports sim: true so the dashboard labels the host "benzetim".
func (a *Actuator) simulate(argv []string) ExecResult {
	if len(argv) == 0 {
		return ExecResult{ExitCode: 127}
	}
	base := filepath.Base(argv[0])
	ok := func(s string) ExecResult { return ExecResult{ExitCode: 0, Stdout: []byte(s)} }
	switch base {
	case "systemctl":
		if len(argv) >= 3 && argv[1] == "show" {
			unit := argv[len(argv)-1]
			st := a.Units.Show(unit)
			if st["LoadState"] != "loaded" {
				return ExecResult{ExitCode: 0, Stdout: []byte("ActiveState=inactive\nSubState=dead\nMainPID=0\nNRestarts=0\n")}
			}
			return ok(fmt.Sprintf("ActiveState=%s\nSubState=%s\nMainPID=%s\nNRestarts=%s\n", st["ActiveState"], st["SubState"], st["MainPID"], st["NRestarts"]))
		}
		if len(argv) >= 3 && argv[1] == "restart" {
			if err := a.Units.SimRestart(argv[len(argv)-1]); err != nil {
				return ExecResult{ExitCode: 5, Stderr: []byte(err.Error())}
			}
			return ok("")
		}
	case "logrotate":
		return ok(fmt.Sprintf("rotating log %s (simulated)\n", strings.Join(argv[1:], " ")))
	case "journalctl":
		return ok("Vacuuming done, freed 0B of archived journals (simulated)\n")
	case "jcmd":
		return ok(" garbage-first heap   total 1048576K, used 524288K\n Metaspace       used 65536K, committed 70000K\n")
	case "lsof_deleted.sh":
		return ok("deleted_open_bytes=0 files=0\n")
	}
	return ok("simulated: " + strings.Join(argv, " ") + "\n")
}
