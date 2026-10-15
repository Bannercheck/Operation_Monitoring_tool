package py

import (
	"strconv"
	"time"
)

// DateTime is a Python datetime with microsecond precision. Aware values carry their own fixed UTC offset
// (never converted); Naive values have no tzinfo and print no offset.
type DateTime struct {
	T     time.Time // microsecond precision; location = time.FixedZone("", offsetSeconds) or time.UTC
	Naive bool      // Python naive datetime (no tzinfo)
}

// Epoch is datetime.fromtimestamp(0, tz=UTC) (common.MISSING_TS).
var Epoch = DateTime{T: time.Unix(0, 0).UTC()}

// NewDateTime builds an aware datetime with the given UTC offset in seconds.
func NewDateTime(year, month, day, hour, min, sec, usec, offsetSec int) DateTime {
	loc := time.UTC
	if offsetSec != 0 {
		loc = time.FixedZone("", offsetSec)
	}
	return DateTime{T: time.Date(year, time.Month(month), day, hour, min, sec, usec*1000, loc)}
}

// NewNaive builds a naive datetime.
func NewNaive(year, month, day, hour, min, sec, usec int) DateTime {
	return DateTime{T: time.Date(year, time.Month(month), day, hour, min, sec, usec*1000, time.UTC), Naive: true}
}

// Offset is utcoffset() in seconds (0 for naive values).
func (d DateTime) Offset() int {
	if d.Naive {
		return 0
	}
	_, off := d.T.Zone()
	return off
}

// IsZero reports the zero value (no datetime).
func (d DateTime) IsZero() bool { return d.T.IsZero() }

func pad(b []byte, v, w int) []byte {
	s := strconv.Itoa(v)
	for i := len(s); i < w; i++ {
		b = append(b, '0')
	}
	return append(b, s...)
}

// ISO is datetime.isoformat(): YYYY-MM-DDTHH:MM:SS[.ffffff][±HH:MM[:SS]] (UTC prints +00:00, never Z).
func (d DateTime) ISO() string {
	t := d.T
	b := make([]byte, 0, 32)
	b = pad(b, t.Year(), 4)
	b = append(b, '-')
	b = pad(b, int(t.Month()), 2)
	b = append(b, '-')
	b = pad(b, t.Day(), 2)
	b = append(b, 'T')
	b = pad(b, t.Hour(), 2)
	b = append(b, ':')
	b = pad(b, t.Minute(), 2)
	b = append(b, ':')
	b = pad(b, t.Second(), 2)
	if us := t.Nanosecond() / 1000; us != 0 {
		b = append(b, '.')
		b = pad(b, us, 6)
	}
	if d.Naive {
		return string(b)
	}
	off := d.Offset()
	if off < 0 {
		b = append(b, '-')
		off = -off
	} else {
		b = append(b, '+')
	}
	b = pad(b, off/3600, 2)
	b = append(b, ':')
	b = pad(b, off/60%60, 2)
	if s := off % 60; s != 0 {
		b = append(b, ':')
		b = pad(b, s, 2)
	}
	return string(b)
}

// Before compares instants (Python aware-datetime ordering). Naive values compare by wall time; Python never mixes
// naive and aware values here (parse_timestamp always returns aware datetimes, P01 section 10.3).
func (d DateTime) Before(o DateTime) bool { return d.T.Before(o.T) }

// Equal compares instants.
func (d DateTime) Equal(o DateTime) bool { return d.T.Equal(o.T) }
