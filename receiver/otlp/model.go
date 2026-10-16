package otlp

// The decoded subset of OTLP. Protobuf and JSON both land here; convert.go turns it into Watchover records.

// Value kinds of an AnyValue.
const (
	VNone = iota
	VStr
	VBool
	VInt
	VFloat
	VArr
	VMap
	VBytes
)

type Any struct {
	Kind  int
	S     string
	B     bool
	I     int64
	F     float64
	Arr   []Any
	KV    []KV
	Bytes []byte
}

type KV struct {
	Key string
	Val Any
}

type Scope struct{ Name, Version string }

type LogRecord struct {
	TimeNs, ObservedNs uint64
	SevNum             int32
	SevText            string
	Body               Any
	Attrs              []KV
	TraceID, SpanID    []byte
	EventName          string
}

type ScopeLogs struct {
	Scope   Scope
	Records []LogRecord
}

type ResourceLogs struct {
	Resource []KV
	Scopes   []ScopeLogs
}

type Span struct {
	TraceID, SpanID, ParentID []byte
	Name                      string
	Kind                      int32 // 0 unspecified, 1 internal, 2 server, 3 client, 4 producer, 5 consumer
	StartNs, EndNs            uint64
	Attrs                     []KV
	StatusCode                int32 // 0 unset, 1 ok, 2 error
	StatusMsg                 string
}

type ScopeSpans struct {
	Scope Scope
	Spans []Span
}

type ResourceSpans struct {
	Resource []KV
	Scopes   []ScopeSpans
}

// Metric shapes Watchover reads: gauges and sums of numbers (histograms and summaries are counted and skipped).
const (
	MOther = iota
	MGauge
	MSum
)

type Point struct {
	Attrs  []KV
	TimeNs uint64
	Value  float64
}

type Metric struct {
	Name, Unit string
	Shape      int
	Monotonic  bool
	Points     []Point
}

type ScopeMetrics struct {
	Scope   Scope
	Metrics []Metric
}

type ResourceMetrics struct {
	Resource []KV
	Scopes   []ScopeMetrics
}

// find returns the value of key in kvs (the first one), and whether it was there.
func find(kvs []KV, key string) (*Any, bool) {
	for i := range kvs {
		if kvs[i].Key == key {
			return &kvs[i].Val, true
		}
	}
	return nil, false
}

// str is the attribute as text ("" when missing or not a scalar).
func str(kvs []KV, keys ...string) string {
	for _, k := range keys {
		if v, ok := find(kvs, k); ok {
			if s := v.Text(); s != "" {
				return s
			}
		}
	}
	return ""
}
