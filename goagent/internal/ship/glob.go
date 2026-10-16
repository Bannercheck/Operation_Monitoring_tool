package ship

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

// pyGlob is Python's glob.glob(pattern) (non-recursive) on POSIX, so --tail patterns and the discovery profiles match the
// same files as in agent.py: a pattern without magic matches itself when it exists (lexists), wildcard segments do not
// match names starting with "." unless the segment does, results come in directory order, fnmatch ("[!x]" negates).
func pyGlob(pattern string) []string {
	var out []string
	pyIGlob(pattern, false, func(p string) { out = append(out, p) })
	return out
}

func hasMagic(s string) bool { return strings.ContainsAny(s, "*?[") }

// pySplit is os.path.split: the head keeps no trailing slash unless it is only slashes.
func pySplit(p string) (string, string) {
	i := strings.LastIndexByte(p, '/') + 1
	head, tail := p[:i], p[i:]
	if head != "" && strings.Trim(head, "/") != "" {
		head = strings.TrimRight(head, "/")
	}
	return head, tail
}

func pyIGlob(pathname string, dironly bool, yield func(string)) {
	dirname, basename := pySplit(pathname)
	if !hasMagic(pathname) {
		if basename != "" {
			if _, err := os.Lstat(pathname); err == nil {
				yield(pathname)
			}
		} else if fi, err := os.Stat(dirname); err == nil && fi.IsDir() {
			yield(pathname)
		}
		return
	}
	if dirname == "" {
		for _, n := range glob1(".", basename, dironly) {
			yield(n)
		}
		return
	}
	var dirs []string
	if dirname != pathname && hasMagic(dirname) {
		pyIGlob(dirname, true, func(d string) { dirs = append(dirs, d) })
	} else {
		dirs = []string{dirname}
	}
	for _, d := range dirs {
		if hasMagic(basename) {
			for _, n := range glob1(d, basename, dironly) {
				yield(pyJoin(d, n))
			}
		} else if _, err := os.Lstat(pyJoin(d, basename)); err == nil { // _glob0
			yield(pyJoin(d, basename))
		}
	}
}

func pyJoin(a, b string) string {
	if a == "" || strings.HasSuffix(a, "/") {
		return a + b
	}
	return a + "/" + b
}

func glob1(dir, pattern string, dironly bool) []string {
	f, err := os.Open(dir)
	if err != nil {
		return nil
	}
	ents, _ := f.ReadDir(-1) // directory order, like os.scandir
	_ = f.Close()
	pat := fnmatchPattern(pattern)
	hidden := strings.HasPrefix(pattern, ".")
	var out []string
	for _, e := range ents {
		n := e.Name()
		if !hidden && strings.HasPrefix(n, ".") {
			continue
		}
		if dironly {
			isDir := e.IsDir()
			if !isDir && e.Type()&os.ModeSymlink != 0 { // scandir's is_dir() follows links
				if fi, err := os.Stat(filepath.Join(dir, n)); err == nil {
					isDir = fi.IsDir()
				}
			}
			if !isDir {
				continue
			}
		}
		if ok, err := filepath.Match(pat, n); err == nil && ok {
			out = append(out, n)
		}
	}
	return out
}

// fnmatchPattern turns fnmatch syntax into filepath.Match syntax: "[!" negates, a backslash is an ordinary character.
func fnmatchPattern(p string) string {
	p = strings.ReplaceAll(p, `\`, `\\`)
	return strings.ReplaceAll(p, "[!", "[^")
}

var envRef = regexp.MustCompile(`\$(\w+|\{[^}]*\})`)

// expandVars is os.path.expandvars: $NAME and ${NAME} when set, untouched otherwise.
func expandVars(p string) string {
	if !strings.Contains(p, "$") {
		return p
	}
	return envRef.ReplaceAllStringFunc(p, func(m string) string {
		name := strings.TrimSuffix(strings.TrimPrefix(m[1:], "{"), "}")
		if v, ok := os.LookupEnv(name); ok {
			return v
		}
		return m
	})
}
