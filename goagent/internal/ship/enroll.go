package ship

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// Enroll is the installer's self-enrolment with the fleet key (POST <base>/enroll, the request agent.py's installer made
// with python3): it returns the collector token, the actuator token and the dashboard's two public keys ("" when not issued).
func Enroll(base, key, name, env, site, tags, caFile string) ([4]string, error) {
	var out [4]string
	cl, err := NewClient(caFile, false, 15*time.Second)
	if err != nil {
		return out, err
	}
	body := obj{{"name", name}, {"env", env}, {"site", site}, {"tags", tags}}.bytes(true)
	req, err := http.NewRequest(http.MethodPost, strings.TrimRight(base, "/")+"/enroll", bytes.NewReader(body))
	if err != nil {
		return out, err
	}
	req.Header.Set("Authorization", "Bearer "+key)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Agent", name)
	resp, err := cl.Do(req)
	if err != nil {
		return out, err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode != http.StatusOK {
		s := string(raw)
		if len(s) > 200 {
			s = s[:200]
		}
		return out, fmt.Errorf("enrolment refused (HTTP %d): %s", resp.StatusCode, s)
	}
	var doc map[string]any
	if err := json.Unmarshal(raw, &doc); err != nil {
		return out, fmt.Errorf("enrolment answer is not JSON: %v", err)
	}
	for i, k := range []string{"token", "actuator_token", "dispatch_pub", "anchor_pub"} {
		if v, ok := doc[k].(string); ok {
			out[i] = strings.TrimSpace(v)
		}
	}
	return out, nil
}
