package act

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"os/user"
	"strconv"
	"syscall"
	"time"
)

// Execution of argv / script templates (§9.3.5): never a shell, stdin /dev/null, cwd "/", a fixed environment, its own process
// group, and the template's run_as user through the kernel credential of the child (the Go runtime sets uid, gid and the
// supplementary groups between fork and exec — the equivalent of Popen(user=, group=, extra_groups=) without preexec_fn).
// On timeout or cancel: SIGTERM to the process group, 10 s grace, SIGKILL.

// Grace between SIGTERM and SIGKILL (a variable so tests need not wait 10 s).
var killGrace = 10 * time.Second

// RunAs is a resolved account.
type RunAs struct {
	Name   string
	UID    uint32
	GID    uint32
	Groups []uint32
}

// LookupRunAs resolves a user name with its primary and supplementary groups.
func LookupRunAs(name string) (*RunAs, error) {
	u, err := user.Lookup(name)
	if err != nil {
		return nil, err
	}
	uid, _ := strconv.ParseUint(u.Uid, 10, 32)
	gid, _ := strconv.ParseUint(u.Gid, 10, 32)
	r := &RunAs{Name: name, UID: uint32(uid), GID: uint32(gid)}
	if gids, err := u.GroupIds(); err == nil {
		for _, g := range gids {
			if n, err := strconv.ParseUint(g, 10, 32); err == nil && uint32(n) != r.GID {
				r.Groups = append(r.Groups, uint32(n))
			}
		}
	}
	return r, nil
}

// limitedBuffer keeps the first max bytes and counts the rest.
type limitedBuffer struct {
	buf       bytes.Buffer
	max       int
	truncated bool
}

func (l *limitedBuffer) Write(p []byte) (int, error) {
	room := l.max - l.buf.Len()
	if room > 0 {
		if len(p) > room {
			l.buf.Write(p[:room])
			l.truncated = true
		} else {
			l.buf.Write(p)
		}
	} else if len(p) > 0 {
		l.truncated = true
	}
	return len(p), nil
}

// ExecResult of one argv run.
type ExecResult struct {
	ExitCode  int
	Stdout    []byte
	Stderr    []byte
	Truncated bool
	TimedOut  bool
	Cancelled bool
}

// RunArgv runs argv without a shell; as (nil = this process's own user), env, output cap and timeout from the template.
func RunArgv(ctx context.Context, argv []string, as *RunAs, env []string, maxOut int, timeout time.Duration) (ExecResult, error) {
	if len(argv) == 0 {
		return ExecResult{}, errors.New("empty argv")
	}
	for _, el := range argv {
		if err := CheckRendered(el); err != nil {
			return ExecResult{}, err
		}
	}
	cmd := exec.Command(argv[0], argv[1:]...)
	cmd.Dir = "/"
	cmd.Env = env
	cmd.Stdin = nil // /dev/null
	out := &limitedBuffer{max: maxOut}
	errb := &limitedBuffer{max: 4096}
	cmd.Stdout, cmd.Stderr = out, errb
	attr := &syscall.SysProcAttr{Setpgid: true}
	if as != nil && (uint32(os.Geteuid()) != as.UID || uint32(os.Getegid()) != as.GID) {
		if os.Geteuid() != 0 {
			return ExecResult{}, fmt.Errorf("running as %s needs root", as.Name)
		}
		attr.Credential = &syscall.Credential{Uid: as.UID, Gid: as.GID, Groups: as.Groups}
	}
	cmd.SysProcAttr = attr
	cmd.WaitDelay = 2 * time.Second // a background grandchild holding stdout cannot keep the job open
	if err := cmd.Start(); err != nil {
		return ExecResult{ExitCode: 127}, err
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	res := ExecResult{}
	var werr error
	select {
	case werr = <-done:
	case <-timer.C:
		res.TimedOut = true
		werr = stopGroup(cmd.Process.Pid, done)
	case <-ctx.Done():
		res.Cancelled = true
		werr = stopGroup(cmd.Process.Pid, done)
	}
	res.Stdout, res.Stderr, res.Truncated = out.buf.Bytes(), errb.buf.Bytes(), out.truncated
	if werr != nil {
		var ee *exec.ExitError
		if errors.As(werr, &ee) {
			res.ExitCode = ee.ExitCode()
			if res.ExitCode < 0 {
				res.ExitCode = 128 + 15
			}
			return res, nil
		}
		return res, werr
	}
	res.ExitCode = cmd.ProcessState.ExitCode()
	return res, nil
}

// stopGroup: SIGTERM to the whole process group, then SIGKILL after the grace period.
func stopGroup(pid int, done chan error) error {
	_ = syscall.Kill(-pid, syscall.SIGTERM)
	select {
	case err := <-done:
		_ = syscall.Kill(-pid, syscall.SIGKILL) // stragglers of the group
		return err
	case <-time.After(killGrace):
		_ = syscall.Kill(-pid, syscall.SIGKILL)
		return <-done
	}
}
