package parse

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"
)

// BenchmarkParseBatch measures IngestBytes on 40-line live batches of each main format. The batches cycle through
// every 40-line window of a 240-line mutated input (fresh timestamps and values), with one long-lived Normalizer
// and MemStore as in the receiver. "detect" runs every batch without a hint (format detection each time); "hint"
// passes the per-stream hint the first batch produced, as live replays do. Reported metric: lines/s.
func BenchmarkParseBatch(b *testing.B) {
	cases := []struct{ format, input string }{
		{"jsonl", "mut__ecs_json.jsonl"},
		{"syslog", "mut__syslog_rfc3164.log"},
		{"access", "mut__nginx_combined.log"},
		{"text", "mut__plain_app_text.log"},
		{"kv", "mut__logfmt.log"},
	}
	for _, c := range cases {
		data, err := os.ReadFile(filepath.Join(inputsDir, c.input))
		if err != nil {
			b.Fatal(err)
		}
		lines := bytes.Split(data, []byte("\n"))
		var chunks [][]byte
		nLines := 0
		for i := 0; i+40 <= len(lines); i += 40 {
			chunks = append(chunks, bytes.Join(lines[i:i+40], []byte("\n")))
			nLines += 40
		}
		for _, mode := range []string{"detect", "hint"} {
			b.Run(c.format+"/"+mode, func(b *testing.B) {
				norm, store := NewNormalizer(2026), NewMemStore()
				var hint *Hint
				if mode == "hint" {
					_, reps, err := IngestBytes(c.input, chunks[0], Options{Norm: norm, Profiles: store})
					if err != nil || len(reps) == 0 || reps[0].Format != c.format {
						b.Fatalf("%s: warm-up batch gave %v / %+v", c.input, err, reps)
					}
					hint = &Hint{Format: reps[0].Format, Confidence: reps[0].Confidence, Family: reps[0].Family, HasFamily: true, N: 1}
				}
				var bytesPer int64
				for _, ch := range chunks {
					bytesPer += int64(len(ch))
				}
				b.SetBytes(bytesPer / int64(len(chunks)))
				b.ResetTimer()
				for i := 0; i < b.N; i++ {
					var h *Hint
					if hint != nil {
						cp := *hint
						h = &cp
					}
					obs, _, err := IngestBytes(c.input, chunks[i%len(chunks)], Options{Hint: h, Norm: norm, Profiles: store})
					if err != nil || len(obs) == 0 {
						b.Fatalf("%s: %v (%d obs)", c.input, err, len(obs))
					}
				}
				b.ReportMetric(float64(b.N)*40/b.Elapsed().Seconds(), "lines/s")
			})
		}
	}
}
