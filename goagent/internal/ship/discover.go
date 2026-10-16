package ship

import (
	"fmt"
	"os"
	"sort"
	"strings"
	"syscall"
	"time"
)

// Automatic log discovery (--auto / WATCHOVER_LOGS=auto): agent.py's PROFILES for Linux and macOS hosts, same detectors,
// same globs, same caps (40 newest files per application, 200 in all). The Windows profiles stay in agent.py.
type profile struct {
	name    string
	markers []string
	globs   []string
}

var profiles = []profile{
	{"SAP NetWeaver ABAP", []string{"/usr/sap/*/D*/work", "/usr/sap/*/DVEBMGS*/work", "/usr/sap/*/ASCS*/work"},
		[]string{"/usr/sap/*/D*/work/dev_w*", "/usr/sap/*/D*/work/dev_disp", "/usr/sap/*/D*/work/dev_rfc*", "/usr/sap/*/D*/work/available.log", "/usr/sap/*/DVEBMGS*/work/dev_w*",
			"/usr/sap/*/DVEBMGS*/work/dev_disp", "/usr/sap/*/ASCS*/work/dev_ms", "/usr/sap/*/ASCS*/work/dev_enq*", "/usr/sap/*/*/work/sapstart.log", "/usr/sap/*/*/work/available.log"}},
	{"SAP NetWeaver Java", []string{"/usr/sap/*/J*/j2ee/cluster", "/usr/sap/*/J*/work"},
		[]string{"/usr/sap/*/J*/j2ee/cluster/server*/log/defaultTrace*.trc", "/usr/sap/*/J*/j2ee/cluster/server*/log/applications*.log", "/usr/sap/*/J*/work/dev_server*", "/usr/sap/*/J*/work/*.jvm", "/usr/sap/*/J*/work/std_server*.out"}},
	{"SAP HANA", []string{"/usr/sap/*/HDB*", "/hana/shared/*/HDB*"},
		[]string{"/usr/sap/*/HDB*/*/trace/indexserver_*.trc", "/usr/sap/*/HDB*/*/trace/nameserver_*.trc", "/usr/sap/*/HDB*/*/trace/xsengine_*.trc", "/usr/sap/*/HDB*/*/trace/*alert*.trc", "/usr/sap/*/HDB*/*/trace/daemon_*.trc"}},
	{"SAP Host Agent", []string{"/usr/sap/hostctrl"}, []string{"/usr/sap/hostctrl/work/dev_saphostexec", "/usr/sap/hostctrl/work/sapstartsrv.log"}},
	{"Oracle Database", []string{"/u01/app/oracle/diag/rdbms", "/opt/oracle/diag/rdbms", "/oracle/*/diag/rdbms", "$ORACLE_BASE/diag/rdbms"},
		[]string{"/u01/app/oracle/diag/rdbms/*/*/trace/alert_*.log", "/opt/oracle/diag/rdbms/*/*/trace/alert_*.log", "/oracle/*/diag/rdbms/*/*/trace/alert_*.log", "$ORACLE_BASE/diag/rdbms/*/*/trace/alert_*.log",
			"/u01/app/oracle/diag/tnslsnr/*/listener/trace/listener.log", "$ORACLE_BASE/diag/tnslsnr/*/*/trace/*.log"}},
	{"Oracle E-Business Suite", []string{"/u01/*/inst/apps", "/u01/install/APPS/inst/apps", "$INST_TOP/logs", "/oracle/*/inst/apps"},
		[]string{"/u01/*/inst/apps/*/logs/appl/conc/log/*.log", "/u01/*/inst/apps/*/logs/appl/conc/log/*.mgr", "/u01/*/inst/apps/*/logs/appl/rgf/*.log", "/u01/*/inst/apps/*/logs/ora/10.1.3/opmn/*.log",
			"/u01/*/inst/apps/*/logs/ora/10.1.3/j2ee/oacore/*.log", "$INST_TOP/logs/appl/conc/log/*.log", "$INST_TOP/logs/ora/10.1.3/opmn/*.log", "/u01/*/fs1/inst/apps/*/logs/appl/conc/log/*.log", "/u01/*/fs2/inst/apps/*/logs/appl/conc/log/*.log"}},
	{"Oracle WebLogic", []string{"/u01/*/user_projects/domains", "/opt/oracle/*/user_projects/domains"},
		[]string{"/u01/*/user_projects/domains/*/servers/*/logs/*.log", "/u01/*/user_projects/domains/*/servers/*/logs/*.out", "/opt/oracle/*/user_projects/domains/*/servers/*/logs/*.log"}},
	{"Apache Tomcat", []string{"/opt/tomcat*", "/usr/share/tomcat*", "/var/log/tomcat*"}, []string{"/opt/tomcat*/logs/catalina.out", "/opt/tomcat*/logs/*.log", "/usr/share/tomcat*/logs/*.log", "/var/log/tomcat*/*.log", "/var/log/tomcat*/catalina.out"}},
	{"JBoss / WildFly", []string{"/opt/wildfly*", "/opt/jboss*"}, []string{"/opt/wildfly*/standalone/log/server.log", "/opt/jboss*/standalone/log/server.log", "/opt/wildfly*/domain/servers/*/log/server.log"}},
	{"IBM WebSphere", []string{"/opt/IBM/WebSphere"}, []string{"/opt/IBM/WebSphere/AppServer/profiles/*/logs/*/SystemOut.log", "/opt/IBM/WebSphere/AppServer/profiles/*/logs/*/SystemErr.log"}},
	{"nginx", []string{"/var/log/nginx"}, []string{"/var/log/nginx/*.log"}},
	{"Apache httpd", []string{"/var/log/httpd", "/var/log/apache2"}, []string{"/var/log/httpd/*log", "/var/log/apache2/*.log"}},
	{"PostgreSQL", []string{"/var/log/postgresql", "/var/lib/pgsql"}, []string{"/var/log/postgresql/*.log", "/var/lib/pgsql/*/data/log/*.log", "/var/lib/pgsql/data/log/*.log"}},
	{"MySQL / MariaDB", []string{"/var/log/mysql", "/var/log/mariadb"}, []string{"/var/log/mysql/*.log", "/var/log/mariadb/*.log", "/var/log/mysqld.log"}},
	{"MongoDB", []string{"/var/log/mongodb"}, []string{"/var/log/mongodb/*.log"}},
	{"Redis", []string{"/var/log/redis"}, []string{"/var/log/redis/*.log"}},
	{"Kafka", []string{"/opt/kafka*", "/var/log/kafka"}, []string{"/opt/kafka*/logs/server.log", "/var/log/kafka/*.log"}},
	{"Elasticsearch", []string{"/var/log/elasticsearch"}, []string{"/var/log/elasticsearch/*.log"}},
	{"Docker containers", []string{"/var/lib/docker/containers"}, []string{"/var/lib/docker/containers/*/*-json.log"}},
	{"Kubernetes node", []string{"/var/log/containers", "/var/log/pods", "/var/lib/kubelet"}, []string{"/var/log/containers/*.log", "/var/log/kube-apiserver.log", "/var/log/kube-scheduler.log", "/var/log/kubelet.log"}},
	{"Linux system", []string{"/var/log"}, []string{"/var/log/syslog", "/var/log/messages", "/var/log/auth.log", "/var/log/secure", "/var/log/kern.log", "/var/log/cron", "/var/log/dmesg"}},
	{"macOS system", []string{"/var/log/system.log"}, []string{"/var/log/system.log", "/var/log/install.log"}},
}

// MaxAutoFiles caps what --auto follows (agent.py MAX_AUTO_FILES).
const MaxAutoFiles = 200

// Found is one detected application and the log files it writes, newest first.
type Found struct {
	App     string
	Files   []string
	Skipped int
}

// root re-roots the profile paths (tests only).
var discoverRoot = ""

func expandPattern(p string) []string {
	p = expandVars(p)
	if strings.Contains(p, "$") {
		return nil
	}
	return pyGlob(discoverRoot + p)
}

// Discover is agent.py's discover_logs(): applications present on this host and their readable log files, newest-modified
// first, 40 per application.
func Discover() []Found {
	var found []Found
	for _, pr := range profiles {
		present := false
		for _, m := range pr.markers {
			if len(expandPattern(m)) > 0 {
				present = true
				break
			}
		}
		if !present {
			continue
		}
		var order []string
		mtime := map[string]time.Time{}
		for _, g := range pr.globs {
			for _, f := range expandPattern(g) {
				fi, err := os.Stat(f)
				if err != nil || !fi.Mode().IsRegular() || syscall.Access(f, 4) != nil {
					continue
				}
				if _, ok := mtime[f]; !ok {
					order = append(order, f)
				}
				mtime[f] = fi.ModTime()
			}
		}
		if len(order) == 0 {
			continue
		}
		sort.SliceStable(order, func(i, j int) bool { return mtime[order[i]].After(mtime[order[j]]) })
		fd := Found{App: pr.name, Files: order}
		if len(order) > 40 {
			fd.Files, fd.Skipped = order[:40], len(order)-40
		}
		found = append(found, fd)
	}
	return found
}

// discoveryBody is agent.py's discovery_report(): one informational event per detected application.
func discoveryBody(found []Found, agent, env string, now time.Time) []byte {
	var b []byte
	for i, f := range found {
		if i > 0 {
			b = append(b, '\n')
		}
		msg := fmt.Sprintf("discovered %s: %d log files", f.App, len(f.Files))
		if f.Skipped > 0 {
			msg += fmt.Sprintf(" (+%d older skipped)", f.Skipped)
		}
		b = obj{{"ts", isoUTC(now)}, {"level", "info"}, {"service", "watchover-agent"}, {"host", agent}, {"env", env}, {"msg", msg},
			{"discovery_app", f.App}, {"discovery_files", f.Files}}.append(b, false)
	}
	return append(b, '\n')
}
