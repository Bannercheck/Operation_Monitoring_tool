// Package otlp is the receiver's OpenTelemetry intake: OTLP logs, traces and metrics over HTTP (/v1/logs, /v1/traces,
// /v1/metrics; protobuf or JSON, gzip) and gRPC, decoded and converted here in Go so the dashboard only stores results.
//
// No dependency: the protobuf wire format is read by hand (varint, fixed32/64, length-delimited), only the fields
// Watchover uses are kept and everything else is skipped, so a batch is decoded in one pass with few allocations.
// Field numbers follow opentelemetry-proto v1 (collector/{logs,trace,metrics}/v1, common/v1, resource/v1).
package otlp

import (
	"encoding/binary"
	"errors"
	"math"
)

var errTrunc = errors.New("otlp: truncated protobuf")

// wire types
const (
	wtVarint = 0
	wtI64    = 1
	wtLen    = 2
	wtI32    = 5
)

type reader struct {
	b []byte
	i int
}

func (r *reader) done() bool { return r.i >= len(r.b) }

func (r *reader) varint() (uint64, error) {
	var x uint64
	for shift := uint(0); shift < 64; shift += 7 {
		if r.i >= len(r.b) {
			return 0, errTrunc
		}
		c := r.b[r.i]
		r.i++
		x |= uint64(c&0x7f) << shift
		if c < 0x80 {
			return x, nil
		}
	}
	return 0, errors.New("otlp: varint overflow")
}

// tag reads the next field key: field number and wire type.
func (r *reader) tag() (int, int, error) {
	k, err := r.varint()
	if err != nil {
		return 0, 0, err
	}
	if k>>3 == 0 || k>>3 > math.MaxInt32 {
		return 0, 0, errors.New("otlp: bad field number")
	}
	return int(k >> 3), int(k & 7), nil
}

func (r *reader) bytes() ([]byte, error) {
	n, err := r.varint()
	if err != nil {
		return nil, err
	}
	if n > uint64(len(r.b)-r.i) {
		return nil, errTrunc
	}
	s := r.b[r.i : r.i+int(n)]
	r.i += int(n)
	return s, nil
}

func (r *reader) fixed64() (uint64, error) {
	if len(r.b)-r.i < 8 {
		return 0, errTrunc
	}
	v := binary.LittleEndian.Uint64(r.b[r.i:])
	r.i += 8
	return v, nil
}

func (r *reader) fixed32() (uint32, error) {
	if len(r.b)-r.i < 4 {
		return 0, errTrunc
	}
	v := binary.LittleEndian.Uint32(r.b[r.i:])
	r.i += 4
	return v, nil
}

// skip passes over a field of wire type wt (groups are not used by OTLP and are refused).
func (r *reader) skip(wt int) error {
	switch wt {
	case wtVarint:
		_, err := r.varint()
		return err
	case wtI64:
		_, err := r.fixed64()
		return err
	case wtLen:
		_, err := r.bytes()
		return err
	case wtI32:
		_, err := r.fixed32()
		return err
	}
	return errors.New("otlp: unsupported wire type")
}

// want checks that a field arrived with the wire type its schema says.
func want(got, exp int) error {
	if got != exp {
		return errors.New("otlp: wrong wire type")
	}
	return nil
}
