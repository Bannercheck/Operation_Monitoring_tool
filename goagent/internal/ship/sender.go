package ship

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// Result of handing a batch to the Sender.
type Result int

const (
	Delivered Result = iota // the receiver answered 200
	Spooled                 // kept in the disk spool, re-sent later
	Dropped                 // refused for good (4xx content error, bad token) or lost (no spool, spool write failed)
	Retry                   // not delivered and no spool: the caller keeps the data and tries again later
)

// permanent: the receiver will never accept this exact batch (agent.py's Sender.PERMANENT).
var permanent = map[int]bool{400: true, 404: true, 405: true, 413: true, 415: true, 422: true}

// Sender POSTs batches to /ingest exactly like agent.py (headers X-Agent, X-File-Name, X-Agent-Version, Bearer token,
// application/octet-stream body) over kept-alive connections. Undeliverable batches go to the spool; while the receiver
// is down or asked us to wait (503 + Retry-After) new batches go straight to the spool, and one drainer re-sends them
// oldest first once it answers again.
type Sender struct {
	URL, Key, Agent, Version string
	Client                   *http.Client
	Spool                    *Spool
	Logf                     func(string, ...any)
	Headers                  func(http.Header) // adds headers to every request (the self-update state, X-Agent-Update)
	OnAnswer                 func(http.Header) // sees the headers of every 2xx answer (the receiver's update hint)

	mu      sync.Mutex
	until   time.Time // no direct sends before this (Retry-After or back-off)
	backoff time.Duration
	lastLog map[string]time.Time
	kick    chan struct{}

	Sent, Failed, SpooledN, DroppedN atomic.Int64
}

// NewClient is the HTTP client: system roots plus the --ca bundle, or no verification with --insecure (testing only).
func NewClient(caFile string, insecure bool, timeout time.Duration) (*http.Client, error) {
	tr := http.DefaultTransport.(*http.Transport).Clone()
	tr.MaxIdleConnsPerHost = 8
	tr.ResponseHeaderTimeout = timeout
	if caFile != "" || insecure {
		cfg := &tls.Config{MinVersion: tls.VersionTLS12}
		if caFile != "" {
			pem, err := os.ReadFile(caFile)
			if err != nil {
				return nil, err
			}
			pool, _ := x509.SystemCertPool()
			if pool == nil {
				pool = x509.NewCertPool()
			}
			if !pool.AppendCertsFromPEM(pem) {
				return nil, errors.New("no certificate in " + caFile)
			}
			cfg.RootCAs = pool
		}
		cfg.InsecureSkipVerify = insecure //nolint:gosec // --insecure is the operator's explicit choice (agent.py parity)
		tr.TLSClientConfig = cfg
	}
	return &http.Client{Transport: tr, Timeout: timeout}, nil
}

func (s *Sender) logf(key, format string, a ...any) {
	if s.Logf == nil {
		return
	}
	if key != "" { // the same complaint at most once a minute
		s.mu.Lock()
		if s.lastLog == nil {
			s.lastLog = map[string]time.Time{}
		}
		if t, ok := s.lastLog[key]; ok && time.Since(t) < time.Minute {
			s.mu.Unlock()
			return
		}
		s.lastLog[key] = time.Now()
		s.mu.Unlock()
	}
	s.Logf(format, a...)
}

type httpError struct {
	code       int
	retryAfter time.Duration
}

func (e *httpError) Error() string { return fmt.Sprintf("HTTP %d", e.code) }

// post sends one batch and returns the receiver's JSON answer.
func (s *Sender) post(ctx context.Context, body []byte, name string) (map[string]any, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, s.URL, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/octet-stream")
	req.Header.Set("X-Agent", s.Agent)
	req.Header.Set("X-File-Name", name)
	req.Header.Set("X-Agent-Version", s.Version)
	req.Header.Set("User-Agent", "wo-agent/"+s.Version)
	req.Header.Set("X-Agent-Engine", "wo-agent")
	if s.Key != "" {
		req.Header.Set("Authorization", "Bearer "+s.Key)
	}
	if s.Headers != nil {
		s.Headers(req.Header)
	}
	resp, err := s.Client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return nil, &httpError{code: resp.StatusCode, retryAfter: retryAfter(resp.Header.Get("Retry-After"))}
	}
	if s.OnAnswer != nil {
		s.OnAnswer(resp.Header)
	}
	out := map[string]any{}
	if len(bytes.TrimSpace(raw)) > 0 {
		_ = json.Unmarshal(raw, &out)
	}
	return out, nil
}

func retryAfter(v string) time.Duration {
	v = strings.TrimSpace(v)
	if v == "" {
		return 0
	}
	if n, err := strconv.Atoi(v); err == nil && n >= 0 {
		return time.Duration(n) * time.Second
	}
	if t, err := http.ParseTime(v); err == nil {
		return time.Until(t)
	}
	return 0
}

// pause stops direct sends for Retry-After, else for a growing back-off (2 s doubling to 60 s).
func (s *Sender) pause(ra time.Duration) time.Duration {
	s.mu.Lock()
	defer s.mu.Unlock()
	d := ra
	if d <= 0 {
		if s.backoff == 0 {
			s.backoff = 2 * time.Second
		} else if s.backoff < time.Minute {
			s.backoff *= 2
			if s.backoff > time.Minute {
				s.backoff = time.Minute
			}
		}
		d = s.backoff
	}
	if d > 10*time.Minute {
		d = 10 * time.Minute
	}
	s.until = time.Now().Add(d)
	return d
}

func (s *Sender) ok() {
	s.mu.Lock()
	s.backoff, s.until = 0, time.Time{}
	s.mu.Unlock()
}

func (s *Sender) paused() (bool, time.Duration) {
	s.mu.Lock()
	defer s.mu.Unlock()
	d := time.Until(s.until)
	return d > 0, d
}

// Send hands one batch over. Delivered, Spooled and Dropped are final; Retry means "keep it and try later" (no spool).
func (s *Sender) Send(ctx context.Context, body []byte, name string) Result {
	r, _ := s.SendAnswer(ctx, body, name)
	return r
}

// SendAnswer is Send that also returns the receiver's JSON answer when it was delivered.
func (s *Sender) SendAnswer(ctx context.Context, body []byte, name string) (Result, map[string]any) {
	if len(body) == 0 {
		return Delivered, nil
	}
	if p, _ := s.paused(); p || (s.Spool != nil && s.Spool.Len() > 0) { // keep the order: behind what is already waiting
		return s.spool(body, name), nil
	}
	out, err := s.post(ctx, body, name)
	if err == nil {
		s.Sent.Add(1)
		s.ok()
		return Delivered, out
	}
	if ctx.Err() != nil { // shutting down, not a receiver problem: keep the batch, do not pause
		return s.spool(body, name), nil
	}
	s.Failed.Add(1)
	var he *httpError
	if errors.As(err, &he) {
		if he.code == 401 || he.code == 403 { // a bad token never gets better by retrying: do not spool
			s.DroppedN.Add(1)
			s.logf("auth", "rejected by the receiver (HTTP %d): check the token", he.code)
			return Dropped, nil
		}
		if he.code == 413 { // agent.py drops it; split it at a line boundary instead while it has more than one line
			if a, b, ok := halves(body); ok {
				r1 := s.Send(ctx, a, name)
				r2 := s.Send(ctx, b, name)
				if r1 == Retry || r2 == Retry {
					return Retry, nil
				}
				return r2, nil
			}
		}
		if permanent[he.code] {
			s.DroppedN.Add(1)
			s.logf("", "batch %s rejected (HTTP %d): dropped", name, he.code)
			return Dropped, nil
		}
		d := s.pause(he.retryAfter)
		s.logf("busy", "receiver answered HTTP %d: holding batches for %s", he.code, d.Round(time.Second))
	} else {
		d := s.pause(0)
		s.logf("net", "send failed: %v (retry in %s)", err, d.Round(time.Second))
	}
	return s.spool(body, name), nil
}

func halves(body []byte) ([]byte, []byte, bool) {
	trimmed := bytes.TrimRight(body, "\n")
	mid := len(trimmed) / 2
	i := bytes.IndexByte(trimmed[mid:], '\n')
	if i < 0 {
		i = bytes.LastIndexByte(trimmed[:mid], '\n')
		if i < 0 {
			return nil, nil, false
		}
		return body[:i+1], body[i+1:], true
	}
	return body[:mid+i+1], body[mid+i+1:], true
}

func (s *Sender) spool(body []byte, name string) Result {
	if s.Spool == nil {
		return Retry
	}
	if err := s.Spool.Put(body, name); err != nil { // disk full / permissions: losing this batch beats stopping the agent
		s.DroppedN.Add(1)
		s.logf("spoolw", "spool write failed: %v", err)
		return Dropped
	}
	s.SpooledN.Add(1)
	s.wake()
	return Spooled
}

func (s *Sender) wake() {
	s.mu.Lock()
	if s.kick == nil {
		s.kick = make(chan struct{}, 1)
	}
	k := s.kick
	s.mu.Unlock()
	select {
	case k <- struct{}{}:
	default:
	}
}

// Drain re-sends spooled batches, oldest first, until ctx ends (one goroutine). A batch refused for good is dropped; a
// network failure or a busy receiver pauses the drain until Retry-After / the back-off has passed.
func (s *Sender) Drain(ctx context.Context) {
	if s.Spool == nil {
		return
	}
	s.wake()
	s.mu.Lock()
	k := s.kick
	s.mu.Unlock()
	t := time.NewTimer(time.Second)
	defer t.Stop()
	for {
		if p, d := s.paused(); p {
			t.Reset(d)
			select {
			case <-ctx.Done():
				return
			case <-t.C:
			}
			continue
		}
		key, name, body, ok := s.Spool.Oldest()
		if !ok {
			t.Reset(5 * time.Second)
			select {
			case <-ctx.Done():
				return
			case <-k:
			case <-t.C:
			}
			continue
		}
		_, err := s.post(ctx, body, name)
		if ctx.Err() != nil {
			return
		}
		if err == nil {
			s.Spool.Remove(key)
			s.Sent.Add(1)
			s.ok()
			continue
		}
		var he *httpError
		if errors.As(err, &he) && (permanent[he.code] || he.code == 401 || he.code == 403) {
			s.Spool.Remove(key)
			s.DroppedN.Add(1)
			if he.code == 401 || he.code == 403 {
				s.logf("auth", "rejected by the receiver (HTTP %d): check the token", he.code)
				s.pause(time.Minute)
			}
			continue
		}
		s.Failed.Add(1)
		if he != nil {
			s.pause(he.retryAfter)
		} else {
			s.pause(0)
		}
	}
}

// Stats is agent.py's periodic counter line.
func (s *Sender) Stats() string {
	dropped := s.DroppedN.Load()
	if s.Spool != nil {
		s.Spool.mu.Lock()
		dropped += s.Spool.Evicted
		s.Spool.mu.Unlock()
	}
	return fmt.Sprintf("sent=%d failed=%d spooled=%d dropped=%d", s.Sent.Load(), s.Failed.Load(), s.SpooledN.Load(), dropped)
}
