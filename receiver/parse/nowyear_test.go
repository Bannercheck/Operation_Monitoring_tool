package parse

import (
	"testing"

	"watchover/receiver/parse/py"
)

// TestNowYear: normalize.py stamps "%b %d %H:%M:%S" with datetime.now().year at call time, while dateutil's year-less
// default is _DEFAULT (the year at import). Probed in Python with datetime.now() patched to 2027-01-01 and _DEFAULT
// from 2026:
//
//	'Oct 14 09:00:01' -> 2027-10-14T09:00:01+00:00   (fast path, call-time year)
//	'Jan  2 00:00:01' -> 2027-01-02T00:00:01+00:00
//	'Oct 14 09:00'    -> 2026-10-14T09:00:00+00:00   (dateutil, import-time year)
//	'14 Oct 09:00:01' -> 2026-10-14T09:00:01+00:00
//
// The receiver sets NowYear to the clock; before, a stage started in 2026 kept stamping syslog lines with 2026 after
// New Year until it was restarted. NowYear nil keeps the golden behaviour (Year for both).
func TestNowYear(t *testing.T) {
	cases := []struct{ in, now, fixed string }{
		{"Oct 14 09:00:01", "2027-10-14T09:00:01+00:00", "2026-10-14T09:00:01+00:00"},
		{"Jan  2 00:00:01", "2027-01-02T00:00:01+00:00", "2026-01-02T00:00:01+00:00"},
		{"Oct 14 09:00", "2026-10-14T09:00:00+00:00", "2026-10-14T09:00:00+00:00"},
		{"14 Oct 09:00:01", "2026-10-14T09:00:01+00:00", "2026-10-14T09:00:01+00:00"},
	}
	clock := NewNormalizer(2026)
	clock.NowYear = func() int { return 2027 }
	fixed := NewNormalizer(2026)
	for _, c := range cases {
		for _, x := range []struct {
			n    *Normalizer
			want string
		}{{clock, c.now}, {fixed, c.fixed}} {
			dt, ok, err := x.n.ParseTimestamp(py.S(c.in))
			if err != nil || !ok || dt.ISO() != x.want {
				t.Errorf("%q (NowYear set %v): %v %v %v, want %s", c.in, x.n.NowYear != nil, dt.ISO(), ok, err, x.want)
			}
		}
	}
}
