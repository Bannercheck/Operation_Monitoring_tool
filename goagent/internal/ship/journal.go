package ship

import (
	"bufio"
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

// Journal streams the systemd journal like agent.py's journal_loop (journalctl -f -o json, one journal.jsonl batch per
// second, same fields), and also keeps the journal cursor of the last batch handed over: a restart continues after it
// instead of losing what was logged meanwhile (agent.py starts at the end every time).
type Journal struct {
	Send       func(ctx context.Context, body []byte, name string) Result
	Agent      string
	CursorFile string
	Logf       func(string, ...any)
	Command    string // journalctl (tests use a stand-in)
}

var journalLevels = map[int]string{0: "critical", 1: "critical", 2: "critical", 3: "error", 4: "warn"}

func jsonText(raw json.RawMessage) (string, bool) {
	if len(raw) == 0 {
		return "", false
	}
	var s string
	if json.Unmarshal(raw, &s) == nil {
		return s, true
	}
	var bs []byte // journald writes a non-UTF-8 / binary field as an array of byte values
	var nums []int
	if json.Unmarshal(raw, &nums) == nil {
		for _, n := range nums {
			bs = append(bs, byte(n))
		}
		return pyText(bs), true
	}
	if string(raw) == "null" {
		return "None", true
	}
	return string(raw), true
}

// journalRecord converts one journalctl JSON line into agent.py's event; ok false for lines agent.py skips.
func journalRecord(line []byte, agent string) (out []byte, cursor string, ok bool) {
	var j map[string]json.RawMessage
	if json.Unmarshal(line, &j) != nil {
		return nil, "", false
	}
	cursor, _ = jsonText(j["__CURSOR"])
	tsS, has := jsonText(j["__REALTIME_TIMESTAMP"])
	if !has {
		tsS = "0"
	}
	us, err := strconv.ParseInt(strings.TrimSpace(tsS), 10, 64)
	if err != nil {
		return nil, cursor, false
	}
	prS, has := jsonText(j["PRIORITY"])
	if !has {
		prS = "6"
	}
	pr, err := strconv.Atoi(strings.TrimSpace(prS))
	if err != nil {
		return nil, cursor, false
	}
	level, ok := journalLevels[pr]
	if !ok {
		level = "info"
	}
	service, _ := jsonText(j["SYSLOG_IDENTIFIER"])
	if service == "" {
		service, _ = jsonText(j["_COMM"])
	}
	if service == "" {
		service = "journal"
	}
	host, has := jsonText(j["_HOSTNAME"])
	if !has {
		host = agent
	}
	msg, _ := jsonText(j["MESSAGE"])
	if utf8.RuneCountInString(msg) > 2000 {
		n := 0
		for i := range msg {
			if n == 2000 {
				msg = msg[:i]
				break
			}
			n++
		}
	}
	o := obj{{"ts", isoUTC(time.UnixMicro(us))}, {"level", level}, {"service", service}, {"host", host}, {"msg", msg}}
	return o.bytes(true), cursor, true
}

func (jr *Journal) loadCursor() string {
	if jr.CursorFile == "" {
		return ""
	}
	b, err := os.ReadFile(jr.CursorFile)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(b))
}

func (jr *Journal) saveCursor(c string) {
	if jr.CursorFile == "" || c == "" {
		return
	}
	_ = os.MkdirAll(filepath.Dir(jr.CursorFile), 0o700)
	tmp := jr.CursorFile + ".tmp"
	if os.WriteFile(tmp, []byte(c+"\n"), 0o600) == nil {
		_ = os.Rename(tmp, jr.CursorFile)
	}
}

// Run streams until ctx ends; journalctl missing ends it quietly (agent.py too).
func (jr *Journal) Run(ctx context.Context) {
	if jr.Logf == nil {
		jr.Logf = func(string, ...any) {}
	}
	cmdName := jr.Command
	if cmdName == "" {
		cmdName = "journalctl"
	}
	path, err := exec.LookPath(cmdName)
	if err != nil {
		return
	}
	cursor := jr.loadCursor()
	pause := 2 * time.Second
	for ctx.Err() == nil {
		started := time.Now()
		usedCursor := cursor != ""
		last, err := jr.stream(ctx, path, cursor)
		if last != "" {
			cursor = last
		}
		if ctx.Err() != nil {
			return
		}
		if usedCursor && time.Since(started) < 5*time.Second && last == "" { // cursor unknown (journal vacuumed): from now on
			jr.Logf("journal cursor not usable (%v): following from now", err)
			cursor = ""
		}
		if time.Since(started) > time.Minute { // journalctl ended (killed, journal rotated): start it again
			pause = 2 * time.Second
		} else if pause < 5*time.Minute { // it keeps failing (no journal access?): no process churn
			pause *= 2
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(pause):
		}
	}
}

func (jr *Journal) stream(ctx context.Context, path, cursor string) (string, error) {
	args := []string{"-f", "-n", "0", "-o", "json"}
	if cursor != "" {
		args = []string{"-f", "-o", "json", "--after-cursor", cursor}
	}
	cctx, cancel := context.WithCancel(ctx)
	defer cancel()
	cmd := exec.CommandContext(cctx, path, args...)
	out, err := cmd.StdoutPipe()
	if err != nil {
		return "", err
	}
	if err := cmd.Start(); err != nil {
		return "", err
	}
	lines := make(chan []byte, 1024)
	go func() {
		defer close(lines)
		r := bufio.NewReaderSize(out, 64<<10)
		for {
			b, err := r.ReadBytes('\n')
			if len(b) > 0 {
				select {
				case lines <- b:
				case <-cctx.Done():
					return
				}
			}
			if err != nil {
				return
			}
		}
	}()
	var buf []byte
	var pendCursor, done string
	flush := func(c context.Context) {
		if len(buf) == 0 {
			return
		}
		if jr.Send(c, buf, "journal.jsonl") != Retry {
			done = pendCursor
			jr.saveCursor(done)
		}
		buf = buf[:0]
	}
	tick := time.NewTicker(time.Second)
	defer tick.Stop()
	for {
		select {
		case b, ok := <-lines:
			if !ok {
				flush(ctx)
				_ = cmd.Wait()
				return done, nil
			}
			rec, c, ok := journalRecord(b, jr.Agent)
			if c != "" {
				pendCursor = c
			}
			if ok {
				buf = append(append(buf, rec...), '\n')
				if len(buf) >= 1<<20 {
					flush(ctx)
				}
			} else if len(buf) == 0 && c != "" { // a skipped line still moves the cursor
				done = c
			}
		case <-tick.C:
			flush(ctx)
		case <-ctx.Done():
			cancel()
			if len(buf) > 0 { // clean shutdown: hand over what was read (spooled if the receiver is away)
				fctx, fcancel := context.WithTimeout(context.Background(), 5*time.Second)
				flush(fctx)
				fcancel()
			}
			_ = cmd.Wait()
			return done, nil
		}
	}
}
