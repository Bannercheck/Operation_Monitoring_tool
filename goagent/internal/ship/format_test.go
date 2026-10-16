package ship

import (
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"
)

// Expected values below were produced by Python 3 (agent.py's own functions / json.dumps / bytes.decode("utf-8", "replace")).

func TestSplitLinesFollowsPythonTextMode(t *testing.T) {
	cases := []struct{ in, out, rest string }{
		{"a\nb\n", "a\nb\n", ""},
		{"a\r\nb\rc\n", "a\nb\nc\n", ""},
		{"a\nfrag", "a\n", "frag"},
		{"a\r", "", "a\r"}, // a final CR may be half of CRLF: wait
		{"a\r\n\r\nb\n", "a\n\nb\n", ""},
		{"\xff\xe2\x82bad\n", "��bad\n", ""},
		{"\xf0\x80\x80x\n", "���x\n", ""},
		{"\xed\xa0\x80y\n", "���y\n", ""},
		{"ok \xc3\xa7\n", "ok \xc3\xa7\n", ""},
		{"\xe0\x80z\n", "��z\n", ""},
		{"\xf4\x90\x80\x80w\n", "����w\n", ""},
	}
	for _, c := range cases {
		out, n := splitLines(nil, []byte(c.in))
		if string(out) != c.out || c.in[n:] != c.rest {
			t.Errorf("%q: got %q rest %q, want %q rest %q", c.in, out, c.in[n:], c.out, c.rest)
		}
	}
	if got := string(fragmentLine(nil, []byte("tail\r"))); got != "tail\n" {
		t.Fatalf("fragment: %q", got)
	}
}

func TestJSONIsPythonsJSONDumps(t *testing.T) {
	o := obj{{"a", "ç \x7f\x01\"\\/😀<>&"}}
	if got := string(o.bytes(true)); got != `{"a": "\u00e7\u2028\u007f\u0001\"\\/\ud83d\ude00<>&"}` {
		t.Fatalf("ascii: %q", got)
	}
	o = obj{{"a", "ç \x7f\x01😀"}}
	if got := string(o.bytes(false)); got != "{\"a\": \"ç \x7f\\u0001😀\"}" {
		t.Fatalf("utf-8: %s", got)
	}
	for in, want := range map[float64]string{0.25: "0.2", 0.35: "0.3", 99.95: "100.0", 12.25: "12.2", 0: "0.0", 100: "100.0", 23.44: "23.4"} {
		if got := string(pyVal(nil, round1(in), true)); got != want {
			t.Errorf("round(%v, 1) = %s, want %s", in, got, want)
		}
	}
	if got := isoUTC(time.Date(2026, 10, 16, 9, 0, 0, 0, time.UTC)); got != "2026-10-16T09:00:00+00:00" {
		t.Fatal(got)
	}
	if got := isoUTC(time.Date(2026, 10, 16, 9, 0, 0, 5000, time.UTC)); got != "2026-10-16T09:00:00.000005+00:00" {
		t.Fatal(got)
	}
	o = obj{{"files", []string{"/a", "/b"}}, {"n", 3}, {"x", nil}}
	if got := string(o.bytes(true)); got != `{"files": ["/a", "/b"], "n": 3, "x": null}` {
		t.Fatal(got)
	}
}

func TestHexAddrMatchesAgentPy(t *testing.T) {
	for in, want := range map[string]string{
		"0100007F:1F90":                         "127.0.0.1:8080",
		"00000000000000000000FFFF0100007F:0050": "0000:0000:0000:0000:ffff:0000:7f00:0001:80",
		"B80D0120000000000000000001000000:01BB": "2001:0db8:0000:0000:0000:0000:0000:0001:443",
		"0000000000000000FFFF00000A00000A:1F90": "10.0.0.10:8080",
	} {
		if got, _ := hexAddr(in); got != want {
			t.Errorf("%s: %s, want %s", in, got, want)
		}
	}
}

func TestPyGlobRules(t *testing.T) {
	d := t.TempDir()
	for _, f := range []string{"a.log", "b.log", ".hidden.log", "c.txt", "sub1/x.log", "sub2/y.log", ".sub/z.log"} {
		p := filepath.Join(d, f)
		_ = os.MkdirAll(filepath.Dir(p), 0o755)
		_ = os.WriteFile(p, []byte("x"), 0o644)
	}
	got := func(p string) string {
		r := pyGlob(p)
		for i := range r {
			r[i] = strings.TrimPrefix(r[i], d+"/")
		}
		sort.Strings(r)
		return strings.Join(r, ",")
	}
	if g := got(d + "/*.log"); g != "a.log,b.log" {
		t.Fatal(g)
	}
	if g := got(d + "/.*.log"); g != ".hidden.log" {
		t.Fatal(g)
	}
	if g := got(d + "/*/*.log"); g != "sub1/x.log,sub2/y.log" {
		t.Fatal(g)
	}
	if g := got(d + "/[!a]*.log"); g != "b.log" {
		t.Fatal(g)
	}
	if g := got(d + "/c.txt"); g != "c.txt" {
		t.Fatal(g)
	}
	if g := got(d + "/nope.txt"); g != "" {
		t.Fatal(g)
	}
	t.Setenv("WO_TEST_BASE", d)
	if g := expandVars("$WO_TEST_BASE/x ${WO_TEST_BASE}/y $UNSET_WO_VAR/z"); g != d+"/x "+d+"/y $UNSET_WO_VAR/z" {
		t.Fatal(g)
	}
}

func TestDiscoverProfiles(t *testing.T) {
	d := t.TempDir()
	discoverRoot = d
	defer func() { discoverRoot = "" }()
	sap := filepath.Join(d, "usr/sap/TPD/D00/work")
	_ = os.MkdirAll(sap, 0o755)
	for i, f := range []string{"dev_w0", "dev_disp", "available.log"} {
		_ = os.WriteFile(filepath.Join(sap, f), []byte("x"), 0o644)
		mt := time.Now().Add(time.Duration(-i) * time.Hour)
		_ = os.Chtimes(filepath.Join(sap, f), mt, mt)
	}
	ora := filepath.Join(d, "u01/app/oracle/diag/rdbms/orcl/orcl/trace")
	_ = os.MkdirAll(ora, 0o755)
	_ = os.WriteFile(filepath.Join(ora, "alert_orcl.log"), []byte("x"), 0o644)
	found := Discover()
	var apps []string
	for _, f := range found {
		apps = append(apps, f.App)
	}
	if strings.Join(apps, "|") != "SAP NetWeaver ABAP|Oracle Database" || len(found[0].Files) != 3 || !strings.HasSuffix(found[0].Files[0], "dev_w0") {
		t.Fatalf("%+v", found)
	}
	body := string(discoveryBody(found[:1], "sap-01", "prod", time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)))
	if !strings.HasPrefix(body, `{"ts": "2026-01-01T00:00:00+00:00", "level": "info", "service": "watchover-agent", "host": "sap-01", "env": "prod", "msg": "discovered SAP NetWeaver ABAP: 3 log files", "discovery_app": "SAP NetWeaver ABAP", "discovery_files": ["`) || !strings.HasSuffix(body, "\"]}\n") {
		t.Fatal(body)
	}
}

func TestJournalRecord(t *testing.T) {
	line := []byte(`{"__CURSOR":"s=1;i=2","__REALTIME_TIMESTAMP":"1760605200123456","PRIORITY":"3","SYSLOG_IDENTIFIER":"sshd","_HOSTNAME":"web-1","MESSAGE":"Failed password for ç"}`)
	out, cur, ok := journalRecord(line, "agent-x")
	if !ok || cur != "s=1;i=2" || string(out) != `{"ts": "2025-10-16T09:00:00.123456+00:00", "level": "error", "service": "sshd", "host": "web-1", "msg": "Failed password for \u00e7"}` {
		t.Fatalf("%v %s %q", ok, cur, out)
	}
	out, _, ok = journalRecord([]byte(`{"__REALTIME_TIMESTAMP":"1","_COMM":"cron","MESSAGE":[104,105]}`), "agent-x")
	if !ok || string(out) != `{"ts": "1970-01-01T00:00:00.000001+00:00", "level": "info", "service": "cron", "host": "agent-x", "msg": "hi"}` {
		t.Fatalf("%s", out)
	}
	if _, _, ok := journalRecord([]byte(`{"PRIORITY":"x"}`), "a"); ok {
		t.Fatal("agent.py skips an entry whose PRIORITY is not a number")
	}
}

func TestMetricsFromProc(t *testing.T) {
	d := t.TempDir()
	procRoot = d
	defer func() { procRoot = getenv("WATCHOVER_PROC", "/proc") }()
	_ = os.WriteFile(filepath.Join(d, "loadavg"), []byte("1.00 0.50 0.25 1/100 999\n"), 0o644)
	_ = os.WriteFile(filepath.Join(d, "meminfo"), []byte("MemTotal:       1000 kB\nMemFree:  10 kB\nMemAvailable:    250 kB\n"), 0o644)
	m := &metricsProbe{ncpu: 4, nvChecked: time.Now()}
	o := m.sample("h1", "prod", time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC))
	got := string(o.bytes(true))
	if !strings.HasPrefix(got, `{"ts": "2026-01-01T00:00:00+00:00", "host": "h1", "cpu": 25.0, "memory": 75.0, "disk": `) || !strings.HasSuffix(got, `, "env": "prod"}`) {
		t.Fatal(got)
	}
}

func TestFlowsAndFingerprintsFromProc(t *testing.T) {
	d := t.TempDir()
	procRoot = d
	defer func() { procRoot = getenv("WATCHOVER_PROC", "/proc") }()
	w := func(p, s string) {
		_ = os.MkdirAll(filepath.Dir(filepath.Join(d, p)), 0o755)
		_ = os.WriteFile(filepath.Join(d, p), []byte(s), 0o644)
	}
	hdr := "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
	w("net/tcp", hdr+
		"   0: 00000000:01BB 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 100 1\n"+
		"   1: 0A00000A:01BB 0B00000A:C350 01 00000000:00000000 00:00000000 00000000     0        0 101 1\n"+
		"   2: 0A00000A:C351 0C00000A:05F1 01 00000000:00000000 00:00000000 00000000     0        0 102 1\n"+
		"   3: 0A00000A:C352 0C00000A:05F1 01 00000000:00000000 00:00000000 00000000     0        0 103 1\n"+
		"   4: 0100007F:C353 0100007F:1F90 01 00000000:00000000 00:00000000 00000000     0        0 104 1\n")
	w("42/comm", "java\n")
	w("42/cmdline", "/usr/bin/java\x00-Dcatalina.base=/opt/tomcat\x00org.apache.catalina.startup.Bootstrap\x00")
	w("42/maps", "7f0-7f1 r-xp 00000000 08:01 1 /opt/oracle/lib/libclntsh.so.19.1\n7f2-7f3 r-xp 0 08:01 2 /usr/lib/jvm/lib/server/libjvm.so\n")
	w("42/stat", "42 (java) S 1 42 42 0 -1 4194560 1 0 0 0 0 0 0 0 20 0 1 0 777 0 0\n")
	_ = os.MkdirAll(filepath.Join(d, "42/fd"), 0o755)
	for fd, ino := range map[string]string{"3": "101", "4": "102", "5": "103"} {
		_ = os.Symlink("socket:["+ino+"]", filepath.Join(d, "42/fd", fd))
	}
	fp := &flowProbe{}
	recs := fp.flows()
	body := string(flowsBody(recs, "h1", "", time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)))
	want := `{"ts": "2026-01-01T00:00:00+00:00", "host": "h1", "msg": "flow out java 10.0.0.12:1521", "kind": "flow", "flow.dir": "out", "flow.process": "java", "flow.remote": "10.0.0.12:1521", "flow.count": 2, "flow.tech": "java,oci,tomcat"}` + "\n" +
		`{"ts": "2026-01-01T00:00:00+00:00", "host": "h1", "msg": "flow in java 443", "kind": "flow", "flow.dir": "in", "flow.process": "java", "flow.port": "443", "flow.count": 1, "flow.tech": "java,oci,tomcat"}` + "\n"
	if body != want {
		t.Fatalf("got\n%s\nwant\n%s", body, want)
	}
}
