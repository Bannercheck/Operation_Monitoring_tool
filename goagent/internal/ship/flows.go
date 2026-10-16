package ship

import (
	"bufio"
	"bytes"
	"encoding/hex"
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// Network flows (who talks to whom) exactly as agent.py's tcp_flows(): established TCP connections from /proc/net/tcp{,6}
// grouped as (direction, process, remote endpoint), loopback skipped, the 400 busiest; each with the technology tags of
// its process matched on the host against a fixed dictionary (only tag names leave the host, never a command line).

// fingerprints: (tag, where, needle), agent.py's FINGERPRINTS in the same order.
var fingerprints = [][3]string{
	{"python", "arg0", "python"}, {"java", "arg0", "java"}, {"java", "map", "libjvm.so"}, {"node", "arg0", "node"}, {"node", "map", "libnode.so"},
	{"dotnet", "arg0", "dotnet"}, {"dotnet", "map", "libcoreclr.so"}, {"php", "arg0", "php"}, {"php", "arg0", "php-fpm"}, {"php", "map", "libphp"}, {"erlang", "arg0", "beam"},
	{"uvicorn", "prog", "uvicorn"}, {"uvicorn", "cmd", "uvicorn.workers"}, {"gunicorn", "prog", "gunicorn"}, {"hypercorn", "prog", "hypercorn"},
	{"uwsgi", "prog", "uwsgi"}, {"fastapi", "prog", "fastapi"},
	{"django", "prog", "manage.py"}, {"django", "prog", "django-admin"}, {"flask", "prog", "flask"}, {"celery", "prog", "celery"},
	{"pydantic_core", "map", "pydantic_core"}, {"uvloop", "map", "/uvloop/"}, {"httptools", "map", "/httptools/"}, {"aiohttp", "map", "/aiohttp/"}, {"tornado", "map", "/tornado/"},
	{"spring-boot", "cmd", "org.springframework.boot"}, {"spring-boot", "cmd", "spring-boot"}, {"tomcat", "cmd", "org.apache.catalina"}, {"tomcat", "cmd", "catalina.base"},
	{"tomcat", "cmd", "tomcat-embed"}, {"weblogic", "cmd", "weblogic.server"}, {"weblogic", "cmd", "weblogic.jar"}, {"jboss", "cmd", "jboss-modules"}, {"jboss", "cmd", "org.jboss.as"},
	{"websphere", "cmd", "com.ibm.ws."}, {"jetty", "cmd", "org.eclipse.jetty"}, {"sap-java", "cmd", "com.sap.engine"}, {"keycloak", "cmd", "-dkc.home.dir="},
	{"kafka-broker", "tok", "kafka.kafka"}, {"zookeeper", "cmd", "org.apache.zookeeper.server"}, {"elasticsearch", "cmd", "org.elasticsearch.bootstrap"},
	{"rabbitmq", "tok", "rabbit"}, {"rabbitmq", "cmd", "rabbitmq_server"}, {"activemq", "cmd", "activemq.jar"},
	{"ojdbc", "cmd", "ojdbc"}, {"oci", "map", "libclntsh"}, {"oracledb", "map", "/oracledb/"}, {"cx_oracle", "map", "cx_oracle"}, {"odp.net", "map", "oracle.manageddataaccess"},
	{"libpq", "map", "libpq.so"}, {"psycopg", "map", "/psycopg"}, {"pgjdbc", "cmd", "postgresql-42"}, {"npgsql", "map", "npgsql.dll"},
	{"mysqlclient", "map", "libmysqlclient"}, {"mariadb-client", "map", "libmariadb"}, {"mysql-connector-j", "cmd", "mysql-connector"},
	{"msodbcsql", "map", "libmsodbcsql"}, {"odbc", "map", "libodbc.so"}, {"pyodbc", "map", "pyodbc"}, {"mssql-jdbc", "cmd", "mssql-jdbc"}, {"sqlclient", "map", "data.sqlclient.dll"},
	{"sapnwrfc", "map", "libsapnwrfc"}, {"sapjco", "map", "libsapjco3"}, {"sapjco", "cmd", "sapjco3"}, {"pyrfc", "map", "/pyrfc/"},
	{"hana-client", "map", "libdbcapihdb"}, {"hana-jdbc", "cmd", "ngdbc"},
	{"librdkafka", "map", "librdkafka"}, {"kafka-clients", "cmd", "kafka-clients"}, {"spring-kafka", "cmd", "spring-kafka"}, {"amqp-client", "cmd", "amqp-client"},
	{"spring-rabbit", "cmd", "spring-rabbit"}, {"grpc", "map", "cygrpc"}, {"grpc", "map", "libgrpc"}, {"grpc", "cmd", "grpc-netty"},
	{"cxf", "cmd", "cxf-"}, {"axis2", "cmd", "axis2"}, {"jaxws", "cmd", "jaxws"}, {"wcf", "map", "system.servicemodel"},
	{"aspnetcore", "map", "microsoft.aspnetcore"}, {"kestrel", "map", "server.kestrel"},
	{"libldap", "map", "libldap"}, {"jsch", "cmd", "jsch"}, {"libssh", "map", "libssh"}, {"sftp", "tok", "sftp"},
	{"nginx", "arg0", "nginx"}, {"apache-httpd", "arg0", "httpd"}, {"apache-httpd", "arg0", "apache2"}, {"haproxy", "arg0", "haproxy"}, {"envoy", "arg0", "envoy"},
	{"traefik", "arg0", "traefik"}, {"postgres", "arg0", "postgres"}, {"postgres", "arg0", "postmaster"}, {"mysql", "arg0", "mysqld"}, {"mariadb", "arg0", "mariadbd"},
	{"redis", "arg0", "redis-server"}, {"mongodb", "arg0", "mongod"}, {"memcached", "arg0", "memcached"}, {"sqlserver", "arg0", "sqlservr"},
	{"oracle-db", "arg0", "tnslsnr"}, {"oracle-db", "arg0", "ora_"}, {"hana", "arg0", "hdbindexserver"}, {"hana", "arg0", "hdbnameserver"},
	{"sap-abap", "arg0", "disp+work"}, {"sap-abap", "arg0", "dw.sap*"}, {"sap-gateway", "arg0", "gwrd"}, {"sap-gateway", "arg0", "gw.sap*"}, {"sap-ms-server", "arg0", "msg_server"},
	{"sap-ms-server", "arg0", "ms.sap*"}, {"sap-icm", "arg0", "icman"},
	{"sap-java", "arg0", "jstart"}, {"openssh", "arg0", "sshd"}, {"openssh", "arg0", "sftp-server"}, {"openldap", "arg0", "slapd"}, {"samba", "arg0", "smbd"},
}

// hexAddr: "0100007F:1F90" -> "127.0.0.1:8080"; IPv6 as eight 4-digit groups, v4-mapped IPv6 as dotted v4 (agent.py _hexaddr).
func hexAddr(h string) (string, bool) {
	ipHex, portHex, ok := strings.Cut(h, ":")
	if !ok {
		return "", false
	}
	port, err := strconv.ParseUint(portHex, 16, 32)
	if err != nil {
		return "", false
	}
	raw, err := hex.DecodeString(ipHex)
	if err != nil || (len(raw) != 4 && len(raw) != 16) {
		return "", false
	}
	var addr string
	if len(raw) == 4 {
		addr = fmt.Sprintf("%d.%d.%d.%d", raw[3], raw[2], raw[1], raw[0])
	} else {
		b := make([]byte, 16)
		for w := 0; w < 4; w++ { // each 32-bit word is little-endian
			for k := 0; k < 4; k++ {
				b[w*4+k] = raw[w*4+3-k]
			}
		}
		if bytes.Equal(b[:12], []byte{0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0xff, 0xff}) {
			addr = fmt.Sprintf("%d.%d.%d.%d", b[12], b[13], b[14], b[15])
		} else {
			parts := make([]string, 8)
			for i := range parts {
				parts[i] = hex.EncodeToString(b[i*2 : i*2+2])
			}
			addr = strings.Join(parts, ":")
		}
	}
	return addr + ":" + strconv.FormatUint(port, 10), true
}

type fpCacheEntry struct{ start, tags string }

// flowProbe keeps the per-process fingerprint cache between samples (pid + start time, like agent.py's _FP_CACHE).
type flowProbe struct {
	cache map[int]fpCacheEntry
	lbuf  []byte
}

func readlinkTo(buf []byte, p string) (string, bool) {
	n, err := syscall.Readlink(p, buf)
	if err != nil || n <= 0 || n >= len(buf) {
		return "", false
	}
	return string(buf[:n]), true
}

// inodeProcess maps socket inode -> process name and pid by walking /proc/<pid>/fd (root, or the same user).
func (fp *flowProbe) inodeProcess() (map[string]string, map[string]int) {
	names, pids := map[string]string{}, map[string]int{}
	d, err := os.Open(procRoot)
	if err != nil {
		return names, pids
	}
	ents, _ := d.Readdirnames(-1) // directory order, like os.listdir
	_ = d.Close()
	if fp.lbuf == nil {
		fp.lbuf = make([]byte, 256)
	}
	for _, pid := range ents {
		if pid == "" || strings.Trim(pid, "0123456789") != "" {
			continue
		}
		comm, err := os.ReadFile(procRoot + "/" + pid + "/comm")
		if err != nil {
			continue
		}
		name := strings.TrimSpace(pyText(comm))
		fdDir := procRoot + "/" + pid + "/fd"
		fd, err := os.Open(fdDir)
		if err != nil {
			continue
		}
		fds, err := fd.Readdirnames(-1)
		_ = fd.Close()
		if err != nil {
			continue
		}
		n, _ := strconv.Atoi(pid)
		for _, f := range fds {
			link, ok := readlinkTo(fp.lbuf, fdDir+"/"+f)
			if ok && strings.HasPrefix(link, "socket:[") && strings.HasSuffix(link, "]") {
				ino := link[8 : len(link)-1]
				names[ino] = name
				pids[ino] = n
			}
		}
	}
	return names, pids
}

func pyText(b []byte) string { return string(appendText(nil, b)) }

func startTime(pid int) string {
	b, err := os.ReadFile(procRoot + "/" + strconv.Itoa(pid) + "/stat")
	if err != nil {
		return ""
	}
	s := pyText(b)
	i := strings.LastIndexByte(s, ')')
	if i < 0 {
		return ""
	}
	f := strings.Fields(s[i+1:])
	if len(f) < 20 {
		return ""
	}
	return f[19] // field 22 (starttime): the pid's identity across reuse
}

func baseName(s string) string {
	if i := strings.LastIndexByte(s, '/'); i >= 0 {
		return s[i+1:]
	}
	return s
}

// splitN6 is bytes.split(None, 5): up to six whitespace-separated fields, the last one the rest of the line.
func splitN6(line []byte) [][]byte {
	var out [][]byte
	i := 0
	for len(out) < 5 {
		for i < len(line) && isSpace(line[i]) {
			i++
		}
		if i >= len(line) {
			return out
		}
		j := i
		for j < len(line) && !isSpace(line[j]) {
			j++
		}
		out = append(out, line[i:j])
		i = j
	}
	for i < len(line) && isSpace(line[i]) {
		i++
	}
	if i < len(line) {
		out = append(out, line[i:])
	}
	return out
}

func isSpace(c byte) bool {
	return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\v' || c == '\f'
}

// fingerprint returns the comma-joined technology tags of one process ("" when nothing matched or nothing is readable).
func (fp *flowProbe) fingerprint(pid int) string {
	if fp.cache == nil {
		fp.cache = map[int]fpCacheEntry{}
	}
	st := startTime(pid)
	if hit, ok := fp.cache[pid]; ok && hit.start == st {
		return hit.tags
	}
	raw, _ := os.ReadFile(procRoot + "/" + strconv.Itoa(pid) + "/cmdline")
	var args []string
	for _, a := range strings.Split(pyText(raw), "\x00") {
		if a != "" {
			args = append(args, strings.ToLower(a))
		}
	}
	first := ""
	if len(args) > 0 {
		first = strings.Split(args[0], " ")[0] // nginx / sshd / postgres rewrite argv into one string
	}
	arg0 := strings.TrimRight(baseName(first), ":")
	toks := map[string]bool{}
	for _, a := range args {
		for _, w := range strings.Split(a, " ") {
			toks[w] = true
			toks[baseName(w)] = true
		}
	}
	prog := map[string]bool{arg0: true}
	for i := 1; i < len(args); {
		a := args[i]
		if a == "-m" && i+1 < len(args) {
			prog[args[i+1]] = true
			break
		}
		if a == "-c" {
			break
		}
		if strings.HasPrefix(a, "-") {
			if a == "-w" || a == "-x" {
				i += 2
			} else {
				i++
			}
			continue
		}
		prog[baseName(a)] = true
		break
	}
	var maps []string
	if f, err := os.Open(procRoot + "/" + strconv.Itoa(pid) + "/maps"); err == nil {
		seen := map[string]bool{}
		sc := bufio.NewScanner(f)
		sc.Buffer(make([]byte, 64<<10), 1<<20)
		for sc.Scan() {
			p := splitN6(sc.Bytes())
			if len(p) == 6 && len(p[5]) > 0 && p[5][0] == '/' {
				m := strings.ToLower(strings.TrimSpace(pyText(p[5])))
				if !seen[m] {
					seen[m] = true
					maps = append(maps, m)
				}
			}
		}
		_ = f.Close()
	}
	tags := map[string]bool{}
	for _, e := range fingerprints {
		tag, where, nd := e[0], e[1], e[2]
		if tags[tag] {
			continue
		}
		hit := false
		switch where {
		case "arg0":
			hit = arg0 == nd || (nd[len(nd)-1] == '*' && strings.HasPrefix(arg0, nd[:len(nd)-1])) ||
				(strings.HasPrefix(arg0, nd) && len(arg0) > len(nd) && (strings.IndexByte("_.", nd[len(nd)-1]) >= 0 || strings.IndexByte("0123456789.", arg0[len(nd)]) >= 0))
		case "prog":
			hit = prog[nd]
		case "tok":
			hit = toks[nd]
		case "cmd":
			for _, a := range args {
				if strings.Contains(a, nd) {
					hit = true
					break
				}
			}
		case "map":
			for _, m := range maps {
				if strings.Contains(m, nd) {
					hit = true
					break
				}
			}
		}
		if hit {
			tags[tag] = true
		}
	}
	list := make([]string, 0, len(tags))
	for t := range tags {
		list = append(list, t)
	}
	sort.Strings(list)
	out := strings.Join(list, ",")
	if len(fp.cache) > 4096 {
		fp.cache = map[int]fpCacheEntry{}
	}
	fp.cache[pid] = fpCacheEntry{st, out}
	return out
}

type flowKey struct{ dir, proc, ep string }

// flows is one tcp_flows() sample: records in agent.py's order (busiest first, ties in first-seen order), at most 400.
func (fp *flowProbe) flows() []obj {
	var rows []string
	for _, f := range []string{procRoot + "/net/tcp", procRoot + "/net/tcp6"} {
		b, err := os.ReadFile(f)
		if err != nil {
			continue
		}
		lines := strings.Split(strings.TrimRight(string(b), "\n"), "\n")
		if len(lines) > 1 {
			rows = append(rows, lines[1:]...)
		}
	}
	if len(rows) == 0 {
		return nil
	}
	type est struct{ local, remote, inode string }
	listen := map[uint64]bool{}
	var ests []est
	for _, r := range rows {
		p := strings.Fields(r)
		if len(p) < 10 {
			continue
		}
		switch p[3] {
		case "0A":
			if _, portHex, ok := strings.Cut(p[1], ":"); ok {
				if port, err := strconv.ParseUint(portHex, 16, 32); err == nil {
					listen[port] = true
				}
			}
		case "01":
			l, ok1 := hexAddr(p[1])
			rm, ok2 := hexAddr(p[2])
			if ok1 && ok2 {
				ests = append(ests, est{l, rm, p[9]})
			}
		}
	}
	procs, pids := fp.inodeProcess()
	counts := map[flowKey]int{}
	var order []flowKey
	tech := map[flowKey]map[string]bool{}
	fps := map[int]string{}
	for _, e := range ests {
		rip := e.remote[:strings.LastIndexByte(e.remote, ':')]
		if strings.HasPrefix(rip, "127.") || rip == "::1" || rip == "0000:0000:0000:0000:0000:0000:0000:0001" {
			continue
		}
		lport, _ := strconv.ParseUint(e.local[strings.LastIndexByte(e.local, ':')+1:], 10, 32)
		proc := procs[e.inode]
		var k flowKey
		if listen[lport] {
			k = flowKey{"in", proc, strconv.FormatUint(lport, 10)}
		} else {
			k = flowKey{"out", proc, e.remote}
		}
		if _, ok := counts[k]; !ok {
			order = append(order, k)
		}
		counts[k]++
		if pid, ok := pids[e.inode]; ok {
			tags, done := fps[pid]
			if !done {
				tags = fp.fingerprint(pid)
				fps[pid] = tags
			}
			if tags != "" {
				if tech[k] == nil {
					tech[k] = map[string]bool{}
				}
				for _, t := range strings.Split(tags, ",") {
					tech[k][t] = true
				}
			}
		}
	}
	for pid := range fp.cache { // exited processes
		if _, ok := fps[pid]; !ok {
			if _, err := os.Stat(procRoot + "/" + strconv.Itoa(pid)); err != nil {
				delete(fp.cache, pid)
			}
		}
	}
	sort.SliceStable(order, func(i, j int) bool { return counts[order[i]] > counts[order[j]] })
	if len(order) > 400 {
		order = order[:400]
	}
	out := make([]obj, 0, len(order))
	for _, k := range order {
		pr := k.proc
		if pr == "" {
			pr = "?"
		}
		epKey := "flow.remote"
		if k.dir == "in" {
			epKey = "flow.port"
		}
		o := obj{{"msg", "flow " + k.dir + " " + pr + " " + k.ep}, {"kind", "flow"}, {"flow.dir", k.dir}, {"flow.process", pr}, {epKey, k.ep}, {"flow.count", counts[k]}}
		if t := tech[k]; len(t) > 0 {
			list := make([]string, 0, len(t))
			for x := range t {
				list = append(list, x)
			}
			sort.Strings(list)
			o = append(o, kv{"flow.tech", strings.Join(list, ",")})
		}
		out = append(out, o)
	}
	return out
}

// flowsBody is one flows.jsonl batch ("" when there is nothing to report).
func flowsBody(recs []obj, host, env string, now time.Time) []byte {
	if len(recs) == 0 {
		return nil
	}
	ts := isoUTC(now)
	var b []byte
	for i, r := range recs {
		if i > 0 {
			b = append(b, '\n')
		}
		o := obj{{"ts", ts}, {"host", host}}
		if env != "" {
			o = append(o, kv{"env", env})
		}
		b = append(o, r...).append(b, true)
	}
	return append(b, '\n')
}
