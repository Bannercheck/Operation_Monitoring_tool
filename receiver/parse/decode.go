package parse

import (
	"bytes"
	"errors"
	"path"
	"strings"
	"unicode/utf8"

	"watchover/receiver/parse/py"
)

// errNoText: loader.iter_bytes yields nothing for this body (binary extension or the NUL guard): Python's ingest
// then returns ([], []) — no observations and no report entry.
var errNoText = errors.New("no text: binary input dropped")

// maxMemberBytes is loader.MAX_MEMBER_BYTES (_Budget.take raises ArchiveTooBig above it).
const maxMemberBytes = 512 << 20

var binaryExt = map[string]bool{".png": true, ".jpg": true, ".jpeg": true, ".gif": true, ".pdf": true, ".parquet": true,
	".xlsx": true, ".xls": true, ".db": true, ".sqlite": true, ".pkl": true, ".so": true}

// pathSuffix is pathlib.PurePosixPath(name).suffix.
func pathSuffix(name string) string {
	base := path.Base(strings.TrimRight(name, "/"))
	if name == "" || base == "." || base == "/" {
		return ""
	}
	i := strings.LastIndexByte(base, '.')
	if i <= 0 || i == len(base)-1 {
		return ""
	}
	return base[i:]
}

// Decode returns the text of a single /ingest body, following loader.iter_bytes + loader.decode (P01 sections 3-4).
// Exact for: binary extensions and the NUL guard (errNoText: nothing yielded), UTF-8 with one leading BOM stripped.
// Archives (.zip/.tar/.tgz/.tar.gz/.gz), UTF-16, the latin-1 fallback and even-length mojibake -> ErrForward;
// a body above 512 MiB -> ErrPyRaise (ArchiveTooBig).
func Decode(name string, data []byte) (string, error) {
	lower := py.Lower(name)
	switch {
	case strings.HasSuffix(lower, ".zip"), strings.HasSuffix(lower, ".tar.gz"), strings.HasSuffix(lower, ".tgz"),
		strings.HasSuffix(lower, ".tar"), strings.HasSuffix(lower, ".gz"):
		return "", forward("archive %q", name)
	}
	if binaryExt[pathSuffix(lower)] {
		return "", errNoText
	}
	if len(data) > maxMemberBytes {
		return "", pyRaise("ArchiveTooBig: a file exceeds %d bytes", maxMemberBytes)
	}
	head := data
	if len(head) > 4096 {
		head = head[:4096]
	}
	if bytes.IndexByte(head, 0) >= 0 && !bytes.HasPrefix(data, []byte{0xff, 0xfe}) && !bytes.HasPrefix(data, []byte{0xfe, 0xff}) {
		return "", errNoText
	}
	body := bytes.TrimPrefix(data, []byte{0xef, 0xbb, 0xbf})
	if !utf8.Valid(body) {
		return "", forward("not UTF-8 (Python falls back to utf-16 / latin-1)")
	}
	return string(body), nil
}
