package parse

import (
	"bytes"
	"sync"
)

// StreamKey identifies a live stream: live.py's _detect key (agent, file base name).
type StreamKey struct{ Agent, File string }

// hintCacheMax: live.py clears the whole cache when it holds more than 5000 streams.
const hintCacheMax = 5000

// hintRedetect: every 200th hinted batch of a stream is parsed without the hint (re-detection).
const hintRedetect = 200

// HintCache is live.py's LiveStore._detect: one global map, mutex-guarded (P01 section 14, P06 section 7.1).
type HintCache struct {
	mu sync.Mutex
	m  map[StreamKey]*Hint
}

// NewHintCache returns an empty cache.
func NewHintCache() *HintCache { return &HintCache{m: map[StreamKey]*Hint{}} }

// Next returns the hint to use for this batch (nil = detect) and increments the stream's counter, exactly like
// live.py: n % 200 == 0 means "no hint for this batch"; the stale entry stays (with its counter) and is replaced only
// by Learn with a non-empty, non-table report. The returned hint is a copy.
func (c *HintCache) Next(k StreamKey) *Hint {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.m == nil {
		c.m = map[StreamKey]*Hint{}
	}
	h := c.m[k]
	if h == nil {
		return nil
	}
	h.N++
	if h.N%hintRedetect == 0 {
		return nil
	}
	cp := *h
	return &cp
}

// Len is the number of cached streams.
func (c *HintCache) Len() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.m)
}

// Get returns a copy of the cached hint of a stream (nil when absent), without counting a batch.
func (c *HintCache) Get(k StreamKey) *Hint {
	c.mu.Lock()
	defer c.mu.Unlock()
	if h := c.m[k]; h != nil {
		cp := *h
		return &cp
	}
	return nil
}

// Invalidate drops the stream's entry (used after a Go parse error, PLAN section 4.2).
func (c *HintCache) Invalidate(k StreamKey) {
	c.mu.Lock()
	defer c.mu.Unlock()
	delete(c.m, k)
}

// asciiSpace is the set Python bytes.strip() / lstrip() remove.
const asciiSpace = " \t\n\r\x0b\x0c"

// learnedHint is the post-parse rule of live.py / parse_golden.py: the hint to store, or nil when the cache must
// not change (a hint was used, no report, or a table report).
func learnedHint(used *Hint, rep []Report, body []byte) *Hint {
	if used != nil || len(rep) == 0 || rep[0].Format == "" || rep[0].Format == "table" {
		return nil
	}
	f := rep[0].Format
	if f == "json" && bytes.Count(bytes.Trim(body, asciiSpace), []byte("\n")) == 0 &&
		bytes.HasPrefix(bytes.TrimLeft(body, asciiSpace), []byte("{")) {
		f = "jsonl" // one object in the first batch of a stream is one JSON line: later batches carry many
	}
	return &Hint{Format: f, Confidence: rep[0].Confidence, Family: rep[0].Family, HasFamily: true, N: 1}
}

// Learn applies the post-parse rule: when the batch was parsed without a hint and produced a non-table report,
// store {format (json -> jsonl single-object rule on the raw body), confidence, family, n: 1}; clear everything when
// the cache exceeds 5000 streams.
func (c *HintCache) Learn(k StreamKey, used *Hint, rep []Report, body []byte) {
	h := learnedHint(used, rep, body)
	if h == nil {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.m == nil {
		c.m = map[StreamKey]*Hint{}
	}
	c.m[k] = h
	if len(c.m) > hintCacheMax {
		c.m = map[StreamKey]*Hint{}
	}
}

// GoldenLive replays the live loop of scripts/parse_golden.py: data split on '\n', batches of `batch` lines joined
// with '\n', whitespace-only batches skipped (no counter increment), the hint dropped entirely at n % 200 == 0.
// It returns the concatenated observations, one report per parsed batch (Report{} for a batch without report) and the
// per-batch "format[:family]" inventory.
func GoldenLive(name string, data []byte, batch int, o Options) (obs []Observation, reps []Report, batchFormats []string, err error) {
	var hint *Hint
	for _, chunk := range liveChunks(data, batch) {
		if hint != nil {
			hint.N++
			if hint.N%hintRedetect == 0 {
				hint = nil
			}
		}
		var use *Hint
		if hint != nil {
			cp := *hint
			use = &cp
		}
		oo := o
		oo.Hint = use
		bo, br, err := IngestBytes(name, chunk, oo)
		if err != nil {
			return obs, reps, batchFormats, err
		}
		if h := learnedHint(use, br, chunk); h != nil {
			hint = h
		}
		obs = append(obs, bo...)
		if len(br) > 0 {
			reps = append(reps, br[0])
			batchFormats = append(batchFormats, FormatKey(br[0].Format, br[0].Family))
		} else {
			reps = append(reps, Report{})
			batchFormats = append(batchFormats, "-")
		}
	}
	return obs, reps, batchFormats, nil
}

// liveChunks is parse_golden.py's batching: data.split(b"\n"), groups of `batch` elements joined with b"\n", chunks
// whose bytes.strip() is empty dropped.
func liveChunks(data []byte, batch int) [][]byte {
	lines := bytes.Split(data, []byte("\n"))
	var out [][]byte
	for i := 0; i < len(lines); i += batch {
		j := i + batch
		if j > len(lines) {
			j = len(lines)
		}
		chunk := bytes.Join(lines[i:j], []byte("\n"))
		if len(bytes.Trim(chunk, asciiSpace)) == 0 {
			continue
		}
		out = append(out, chunk)
	}
	return out
}
