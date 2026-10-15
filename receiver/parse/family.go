package parse

import (
	"watchover/receiver/parse/py"
)

// Port of src/watchover/parsers/family.py (P03 section 4): FAMILIES (24, ordered), STRONG, detect_family.
//
// All family patterns are used with search() as booleans only, so every Python Unicode \b next to a word character is
// rewritten as a consuming boundary: \bX -> (?:^|[^\w])X, X\b -> X(?:[^\w]|$). The trailing \b of \bSysOperation\w*\b
// and \bDMF\w*\b is redundant (the greedy \w* always ends on a boundary) and dropped.

const (
	famLB = `(?:^|[^\w])`
	famRB = `(?:[^\w]|$)`
)

type famDef struct {
	name string
	pats []litFilter
}

func famPats(ps ...string) []litFilter {
	out := make([]litFilter, 0, len(ps))
	for _, p := range ps {
		out = append(out, newLitFilter(py.MustCompile(p)))
	}
	return out
}

// families is FAMILIES in list order (an earlier family keeps a tie).
var families = []famDef{
	{"postgres", famPats(`\] (?:[\w@]+ )?(?:LOG|ERROR|FATAL|DETAIL|STATEMENT|HINT|WARNING|PANIC):  `)},
	{"mysql", famPats(`\[MY-\d{6}\]`, famLB+`mysqld`+famRB)},
	{"sqlserver", famPats(`^\S+ \S+ +(?:spid\d+s?|Logon|Backup|Server) +`, `Microsoft SQL Server`)},
	{"oracle-ebs", famPats(famLB + `APP-FND-\d{5}` + famRB + `|` + famLB + `Concurrent Manager` + famRB + `|\*\*Starts\*\*|` + famLB + `Request ID:|` + famLB + `FNDLIBR` + famRB + `|` + famLB + `APPLCSF` + famRB + `|` + famLB + `OACORE` + famRB + `|` + famLB + `WLS_FORMS` + famRB)},
	{"oracle", famPats(famLB + `ORA-\d{5}` + famRB + `|` + famLB + `TNS-\d{5}` + famRB + `|` + famLB + `LGWR` + famRB + `|` + famLB + `ARC\d` + famRB + `|` + famLB + `alert_\w+\.log` + famRB + `|advanced to log sequence`)},
	{"dynamics-ax", famPats(`Microsoft\.Dynamics\.AX|` + famLB + `Ax32Serv` + famRB + `|` + famLB + `AOS` + famRB + `.*(?:session|instance)|Dynamics AX`)},
	{"dynamics-365", famPats(`Microsoft\.Dynamics\.(?:365|Ax\.Xpp|Platform)|` + famLB + `D365` + famRB + `|` + famLB + `SysOperation\w*|Dynamics 365|` + famLB + `BatchJob` + famRB + `|` + famLB + `DMF\w*`)},
	{"dynamics-nav", famPats(`Microsoft Dynamics NAV|Business Central|` + famLB + `NavServer` + famRB + `|` + famLB + `Microsoft\.Dynamics\.Nav` + famRB + `|` + famLB + `NAV Server` + famRB + `|` + famLB + `BC\d{2}` + famRB)},
	{"sap", famPats(famLB + `SAP` + famRB + `|` + famLB + `ABAP` + famRB + `|` + famLB + `NetWeaver` + famRB + `|` + famLB + `HANA` + famRB + `|` + famLB + `SM21` + famRB + `|` + famLB + `disp\+work` + famRB)},
	{"redis", famPats(`^\d+:[MCSX] \d{2} \w{3} \d{4}`)},
	{"nginx", famPats(famLB + `nginx` + famRB + `|^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} \[\w+\] \d+#\d+:`)},
	{"apache", famPats(`\[(?:core|mpm_\w+|ssl|proxy\w*|rewrite):\w+\] \[pid \d+`)},
	{"kafka", famPats(`\(kafka\.[\w.]+\)|` + famLB + `KafkaServer` + famRB + `|` + famLB + `ReplicaManager` + famRB)},
	{"zookeeper", famPats(`\(org\.apache\.zookeeper`)},
	{"elasticsearch", famPats(`\[o\.e\.[\w.]+\]|` + famLB + `Elasticsearch` + famRB)},
	{"haproxy", famPats(famLB + `haproxy\[\d+\]`)},
	{"postfix", famPats(famLB + `postfix/\w+\[\d+\]`)},
	{"sshd", famPats(famLB + `sshd\[\d+\]`)},
	{"iis", famPats(`^#Software: Microsoft Internet Information Services`)},
	{"windows", famPats(`<Event xmlns=|EventID|Event ID`)},
	{"java", famPats(`^\s+at [\w$.]+\([\w.]*:\d+\)|^[\w.]+Exception: |` + famLB + `common frames omitted` + famRB)},
	{"python", famPats(`^Traceback \(most recent call last\):|^  File "[^"]+", line \d+`)},
	{"dotnet", famPats(`^\s+at [\w.]+\.[\w` + "`" + `]+\(.*\) in .*\.cs:line \d+|^System\.[\w.]+Exception`)},
	{"golang", famPats(`^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} \w+\.go:\d+:|^goroutine \d+ \[`)},
}

// strongFamilies is STRONG: a single hit wins when the family has the most hits.
var strongFamilies = map[string]bool{"oracle-ebs": true, "dynamics-ax": true, "dynamics-365": true, "dynamics-nav": true,
	"sap": true, "iis": true, "redis": true, "postgres": true, "mysql": true, "sqlserver": true, "kafka": true,
	"haproxy": true, "postfix": true, "java": true, "python": true, "dotnet": true, "golang": true}

// detectFamily is family.detect_family(lines) with the contextvars made explicit: a non-nil fc.Hint (even "") is
// returned verbatim and recorded in fc.Seen; otherwise the family is detected and recorded.
func detectFamily(lines []string, fc *FamilyCtx) string {
	var fam string
	if fc != nil && fc.Hint != nil {
		fam = *fc.Hint
	} else {
		fam = detectFamilyLines(lines)
	}
	if fc != nil {
		f := fam
		fc.Seen = &f
	}
	return fam
}

// detectFamilyLines is family._detect_family(lines): the family with the most matching lines among the non-blank
// lines of lines[:200]; "" when nothing reaches the threshold.
func detectFamilyLines(lines []string) string {
	if len(lines) > 200 {
		lines = lines[:200]
	}
	sample := make([]string, 0, len(lines))
	for _, ln := range lines {
		if !isBlank(ln) {
			sample = append(sample, ln)
		}
	}
	if len(sample) == 0 {
		return ""
	}
	best, bestHits := "", 0
	for _, f := range families {
		hits := 0
		for _, ln := range sample {
			for _, p := range f.pats {
				if p.MatchString(ln) {
					hits++
					break
				}
			}
		}
		if hits > bestHits {
			best, bestHits = f.name, hits
		}
	}
	if best == "" {
		return ""
	}
	if strongFamilies[best] && bestHits >= 1 {
		return best
	}
	if float64(bestHits)/float64(len(sample)) >= 0.3 {
		return best
	}
	return ""
}
