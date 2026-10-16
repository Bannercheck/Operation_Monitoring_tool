package ship

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestJournalStreamKeepsItsCursor(t *testing.T) {
	d := t.TempDir()
	fake := filepath.Join(d, "journalctl")
	script := "#!/bin/sh\necho \"$*\" >> " + filepath.Join(d, "args") + "\n" +
		`echo '{"__CURSOR":"c1","__REALTIME_TIMESTAMP":"1000000","PRIORITY":"4","SYSLOG_IDENTIFIER":"kernel","MESSAGE":"one"}'` + "\n" +
		`echo '{"__CURSOR":"c2","__REALTIME_TIMESTAMP":"2000000","PRIORITY":"6","_COMM":"cron","MESSAGE":"two"}'` + "\n" +
		"exec sleep 30\n"
	if err := os.WriteFile(fake, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	cur := filepath.Join(d, "journal.cursor")
	run := func(rec *recorder) {
		ctx, cancel := context.WithCancel(context.Background())
		done := make(chan struct{})
		go func() { (&Journal{Send: rec.send, Agent: "h", CursorFile: cur, Command: fake}).Run(ctx); close(done) }()
		deadline := time.Now().Add(5 * time.Second)
		for len(rec.lines("journal.jsonl")) < 2 && time.Now().Before(deadline) {
			time.Sleep(20 * time.Millisecond)
		}
		cancel()
		<-done
	}
	rec := &recorder{}
	run(rec)
	got := rec.lines("journal.jsonl")
	if len(got) != 2 || got[0] != `{"ts": "1970-01-01T00:00:01+00:00", "level": "warn", "service": "kernel", "host": "h", "msg": "one"}` {
		t.Fatalf("%q", got)
	}
	if b, _ := os.ReadFile(cur); strings.TrimSpace(string(b)) != "c2" {
		t.Fatalf("cursor %q", b)
	}
	run(&recorder{}) // the fake replays both entries; what matters is how it was asked
	args, _ := os.ReadFile(filepath.Join(d, "args"))
	lines := strings.Split(strings.TrimSpace(string(args)), "\n")
	if len(lines) != 2 || lines[0] != "-f -n 0 -o json" || lines[1] != "-f -o json --after-cursor c2" {
		t.Fatalf("journalctl calls %q", lines)
	}
}
