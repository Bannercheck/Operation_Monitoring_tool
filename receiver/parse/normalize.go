package parse

import (
	"math"
	"math/big"
	"sync"
	"time"

	"watchover/receiver/parse/py"
)

// Owner: WP2 (P02). The Normalizer holds the process-global caches of normalize.py (mutex-guarded) and the injected
// "current year".

// tsFlag marks parse_timestamp results that Go can represent but must not emit as a parsed observation.
type tsFlag uint8

const (
	tsPlain tsFlag = iota
	// tsBadOffset: a dateutil tzoffset with |offset| >= 24h. parse_timestamp returns it, but every later utcoffset()
	// (sort, isoformat) raises ValueError in Python. auto_map only tests "is not None", so it counts there.
	tsBadOffset
	// tsSubSecOffset: a UTC offset with microseconds (fromisoformat "+03:00:30.5", strptime %z fraction). Python
	// prints it as +HH:MM:SS.ffffff; py.DateTime cannot hold it, so the batch is forwarded.
	tsSubSecOffset
	// tsHostTZ: dateutil met a time zone name; the Python result depends on the host's time.tzname (Strict only).
	tsHostTZ
)

// tsResult is one memoised parse_timestamp result (ok == false is None).
type tsResult struct {
	dt   py.DateTime
	ok   bool
	flag tsFlag
}

type sevKey struct {
	v     string // py.Value.HashKey() (str(value) for non-scalars)
	scale bool
}

const (
	tsCacheMax  = 200_000
	sevCacheMax = 50_000
	envCacheMax = 50_000
)

// Normalizer replaces normalize.py's module state.
type Normalizer struct {
	Year        int        // datetime.now().year at import (normalize._DEFAULT, the dateutil default); goldens: 2026
	NowYear     func() int // datetime.now().year per call ("%b %d %H:%M:%S" fast path); nil = Year (goldens, tests)
	mu          sync.Mutex
	tsCache     map[string]tsResult // cleared at 200_000 (P02 section 3.1)
	formatCache map[string]int      // shape -> _FAST_FORMATS index; never cleared (P02 section 3.6)
	sevCache    map[sevKey]string   // cap 50_000, no clear (P02 section 6)
	envCache    map[[3]string]string
	// Strict is true in the receiver: a dateutil result that names a time zone (other than UTC/GMT/Z) depends on the
	// Python host's time.tzname, so ParseTimestamp returns ErrForward for it. Goldens run with Strict == false.
	Strict bool
}

// NewNormalizer returns a Normalizer with empty caches.
func NewNormalizer(year int) *Normalizer {
	return &Normalizer{
		Year:        year,
		tsCache:     map[string]tsResult{},
		formatCache: map[string]int{},
		sevCache:    map[sevKey]string{},
		envCache:    map[[3]string]string{},
	}
}

// nowYear is the year the syslog fast path stamps (normalize.py: dt.replace(year=datetime.now().year)).
func (n *Normalizer) nowYear() int {
	if n.NowYear != nil {
		return n.NowYear()
	}
	return n.Year
}

// ParseTimestamp is normalize.parse_timestamp(v): (datetime, true) or (_, false) for None; error = ErrPyRaise /
// ErrForward. Results are always aware (naive -> UTC).
func (n *Normalizer) ParseTimestamp(v py.Value) (py.DateTime, bool, error) {
	r, err := n.parseTS(v)
	if err != nil || !r.ok {
		return py.DateTime{}, false, err
	}
	switch r.flag {
	case tsBadOffset:
		return py.DateTime{}, false, pyRaise("ValueError: utcoffset() out of range (%s)", v.Str())
	case tsSubSecOffset:
		return py.DateTime{}, false, forward("UTC offset with microseconds (%s)", v.Str())
	case tsHostTZ:
		if n.Strict {
			return py.DateTime{}, false, forward("dateutil time zone name depends on the host (%s)", v.Str())
		}
	}
	return r.dt, true, nil
}

// parseTS is parse_timestamp with the result flag kept (auto_map's predicate needs "is not None" only).
func (n *Normalizer) parseTS(v py.Value) (tsResult, error) {
	switch v.K {
	case py.KNone:
		return tsResult{}, nil
	case py.KBool, py.KInt, py.KFloat:
		return numericTS(v)
	}
	s := py.Strip(v.Str())
	if s == "" {
		return tsResult{}, nil
	}
	n.mu.Lock()
	hit, ok := n.tsCache[s]
	n.mu.Unlock()
	if ok {
		return hit, nil
	}
	r, err := n.parseTimestampStr(s)
	if err != nil {
		return tsResult{}, err // Python: the exception escapes, nothing is cached
	}
	n.mu.Lock()
	if len(n.tsCache) >= tsCacheMax {
		clear(n.tsCache)
	}
	n.tsCache[s] = r
	n.mu.Unlock()
	return r, nil
}

// numericTS is the (int, float, bool) branch: v = float(value); fromtimestamp(v/1000 if v > 1e12 else v, tz=UTC).
func numericTS(v py.Value) (tsResult, error) {
	var f float64
	switch v.K {
	case py.KBool:
		if v.B {
			f = 1
		}
	case py.KInt:
		if v.Big != nil {
			f, _ = new(big.Float).SetInt(v.Big).Float64() // correctly rounded, half-even
			if math.IsInf(f, 0) {
				return tsResult{}, pyRaise("OverflowError: int too large to convert to float")
			}
		} else {
			f = float64(v.I)
		}
	case py.KFloat:
		f = v.F
	}
	if f > 1e12 {
		f /= 1000
	}
	dt, err := fromTimestampFloat(f)
	if err != nil {
		return tsResult{}, err
	}
	return tsResult{dt: dt, ok: true}, nil
}

// Supported datetime range in Unix seconds: 0001-01-01T00:00:00 .. 9999-12-31T23:59:59.
const (
	minUnixSec = -62135596800
	maxUnixSec = 253402300799
)

// fromTimestampFloat is datetime.fromtimestamp(d, tz=UTC) for a float (CPython _PyTime_ObjectToTimeval, ROUND_HALF_EVEN).
func fromTimestampFloat(d float64) (py.DateTime, error) {
	if math.IsNaN(d) {
		return py.DateTime{}, pyRaise("ValueError: Invalid value NaN (not a number)")
	}
	if math.IsInf(d, 0) {
		return py.DateTime{}, pyRaise("OverflowError: cannot convert float infinity to integer")
	}
	ip, frac := math.Modf(d)
	us := math.RoundToEven(frac * 1e6)
	if us >= 1e6 {
		us -= 1e6
		ip++
	} else if us < 0 {
		us += 1e6
		ip--
	}
	if ip < minUnixSec || ip > maxUnixSec {
		return py.DateTime{}, pyRaise("ValueError: year is out of range (timestamp %v)", d)
	}
	return py.DateTime{T: time.Unix(int64(ip), int64(us)*1000).UTC()}, nil
}

// fromTimestampRat is fromtimestamp(int(s) / den): the exact quotient rounded once to a double, then the float path.
func fromTimestampRat(num *big.Int, den int64) (py.DateTime, error) {
	f, _ := new(big.Rat).SetFrac(num, big.NewInt(den)).Float64()
	return fromTimestampFloat(f)
}

// fromTimestampInt is fromtimestamp(int, tz=UTC) (exact seconds).
func fromTimestampInt(num *big.Int) (py.DateTime, error) {
	if !num.IsInt64() || num.Int64() < minUnixSec || num.Int64() > maxUnixSec {
		return py.DateTime{}, pyRaise("ValueError: year is out of range (timestamp %s)", num)
	}
	return py.DateTime{T: time.Unix(num.Int64(), 0).UTC()}, nil
}

// aware is `dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt`.
func aware(dt py.DateTime) py.DateTime {
	if dt.Naive {
		return py.DateTime{T: time.Date(dt.T.Year(), dt.T.Month(), dt.T.Day(), dt.T.Hour(), dt.T.Minute(), dt.T.Second(),
			dt.T.Nanosecond(), time.UTC)}
	}
	return dt
}

// daysIn is calendar.monthrange(year, month)[1] (proleptic Gregorian, any year).
func daysIn(year, month int64) int64 {
	switch month {
	case 1, 3, 5, 7, 8, 10, 12:
		return 31
	case 4, 6, 9, 11:
		return 30
	}
	if isLeap(year) {
		return 29
	}
	return 28
}

func isLeap(y int64) bool { return y%4 == 0 && (y%100 != 0 || y%400 == 0) }

// validDate is the datetime(...) constructor check for the date and time fields.
func validDateTime(y, mo, d, h, mi, s, us int64) bool {
	return y >= 1 && y <= 9999 && mo >= 1 && mo <= 12 && d >= 1 && d <= daysIn(y, mo) &&
		h >= 0 && h <= 23 && mi >= 0 && mi <= 59 && s >= 0 && s <= 59 && us >= 0 && us <= 999999
}

// mkDateTime builds a datetime; offset == nil is naive. Fixed offsets are whole seconds.
func mkDateTime(y, mo, d, h, mi, s, us int64, offset *int64) py.DateTime {
	if offset == nil {
		return py.NewNaive(int(y), int(mo), int(d), int(h), int(mi), int(s), int(us))
	}
	return py.NewDateTime(int(y), int(mo), int(d), int(h), int(mi), int(s), int(us), int(*offset))
}
