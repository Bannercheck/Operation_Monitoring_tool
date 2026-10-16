package ship

import (
	"context"
	"io"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"
)

// Tailer follows every file matching its patterns like agent.py's tail_loop (tail -F): one goroutine polls all files,
// ships complete lines only, one batch per file per flush (X-File-Name = the file's base name), survives rename rotation
// (the old file is drained, the new one read from its start) and copytruncate (read again from the start), and re-scans
// the globs. Unlike agent.py it keeps positions on disk: a restart resumes where the receiver's acknowledgement (or the
// spool) left off, and finds a file rotated while it was stopped by its inode.
//
// Start policy, as agent.py: a file seen for the first time is followed from its end (no backlog flood from big existing
// logs). Files that appear after that (rotation, a new dated file, or a file created while the agent was stopped under a
// known pattern) are read from their start, except copies of content already shipped and files larger than BigNew.
type Tailer struct {
	Patterns []string
	Send     func(ctx context.Context, body []byte, name string) Result
	Logf     func(string, ...any)
	State    string // positions file ("" = memory only, like agent.py)
	Agent    string

	Poll, FlushEvery, ScanEvery, RotateCheck, RotatedGrace time.Duration
	MaxBatch, MaxLine, Parallel                            int
	BigNew                                                 int64

	reg          *registry
	open         map[fileID]*harvester
	byPath       map[string]*harvester
	list         []*harvester
	listDirty    bool
	buf          []byte
	warned       map[string]bool
	bad          map[string]bool
	lastScan     time.Time
	lastFlush    time.Time
	wantScan     bool
	rotatedPaths map[string]bool
	lastSaveErr  time.Time
}

type harvester struct {
	id        fileID
	key       string // the path whose lines it carries (its batch goes out with that path's, older file first)
	path      string // where it is read from
	name      string // X-File-Name (the original base name, also after a rotation)
	f         *os.File
	rd        int64  // offset after the last byte read
	pendEnd   int64  // offset up to which pend holds the lines (rd minus the unfinished fragment)
	partial   []byte // trailing fragment without a line end
	pend      []byte // complete lines waiting for the next flush
	rotated   bool
	closing   bool
	lastGrow  time.Time
	lastCheck time.Time
	lastRot   time.Time
	e         *posEntry
}

func (t *Tailer) defaults() {
	if t.Poll == 0 {
		t.Poll = 250 * time.Millisecond
	}
	if t.FlushEvery == 0 {
		t.FlushEvery = time.Second
	}
	if t.ScanEvery == 0 {
		t.ScanEvery = 10 * time.Second
	}
	if t.RotateCheck == 0 {
		t.RotateCheck = time.Second
	}
	if t.RotatedGrace == 0 {
		t.RotatedGrace = 5 * time.Second
	}
	if t.MaxBatch == 0 {
		t.MaxBatch = 1 << 20
	}
	if t.MaxLine == 0 {
		t.MaxLine = 1 << 20
	}
	if t.Parallel == 0 {
		t.Parallel = 4
	}
	if t.BigNew == 0 {
		t.BigNew = 64 << 20
	}
	if t.Logf == nil {
		t.Logf = func(string, ...any) {}
	}
	t.reg = loadRegistry(t.State)
	t.open = map[fileID]*harvester{}
	t.byPath = map[string]*harvester{}
	t.buf = make([]byte, 64<<10)
	t.warned = map[string]bool{}
	t.bad = map[string]bool{}
	t.rotatedPaths = map[string]bool{}
}

// Seed records every matching file at its current end and saves the positions (the installer's hand-over from agent.py,
// which keeps none): the service started next ships exactly what is written from now on. An existing positions file wins.
func (t *Tailer) Seed() (int, error) {
	t.defaults()
	if t.reg.hadState {
		return -1, nil
	}
	t.scan(time.Now(), true)
	n := len(t.open)
	err := t.save()
	t.closeAll()
	return n, err
}

// Run follows the files until ctx ends, then reads what is there, flushes it (failures go to the spool) and saves.
func (t *Tailer) Run(ctx context.Context) {
	t.defaults()
	now := time.Now()
	t.scan(now, true)
	t.recoverRotated(now)
	t.lastFlush = now
	if t.reg.dirty {
		t.saveLog()
	}
	timer := time.NewTimer(t.Poll)
	defer timer.Stop()
	for {
		now = time.Now()
		more := t.poll(now, true)
		if t.wantScan || now.Sub(t.lastScan) >= t.ScanEvery {
			t.scan(now, false)
		}
		held := false
		if t.flushDue(now) {
			held = t.flush(ctx)
		} else if t.reg.dirty {
			t.saveLog()
		}
		if ctx.Err() != nil {
			break
		}
		if more && !held { // a full batch was cut: read on at once
			continue
		}
		timer.Reset(t.Poll)
		select {
		case <-ctx.Done():
		case <-timer.C:
		}
	}
	// clean shutdown: what is already written goes out (or into the spool) and the positions say so
	fctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	for i := 0; i < 64; i++ {
		more := t.poll(time.Now(), false)
		held := t.flush(fctx)
		if !more || held || fctx.Err() != nil {
			break
		}
	}
	t.saveLog()
	t.closeAll()
}

func (t *Tailer) closeAll() {
	for _, h := range t.open {
		_ = h.f.Close()
	}
}

func (t *Tailer) saveLog() {
	if err := t.save(); err != nil && time.Since(t.lastSaveErr) > time.Minute {
		t.lastSaveErr = time.Now()
		t.Logf("positions not saved: %v", err)
	}
}

func (t *Tailer) save() error {
	for id, e := range t.reg.entries {
		h := t.open[id]
		e.Active = h != nil && !h.rotated
	}
	return t.reg.save(t.Patterns, t.Agent)
}

func (t *Tailer) harvesters() []*harvester {
	if t.listDirty || t.list == nil {
		t.list = t.list[:0]
		for _, h := range t.open {
			t.list = append(t.list, h)
		}
		sort.Slice(t.list, func(i, j int) bool { return t.list[i].path < t.list[j].path })
		t.listDirty = false
	}
	return t.list
}

// scan matches the patterns (Python glob rules) and starts a harvester for every file not followed yet.
func (t *Tailer) scan(now time.Time, initial bool) {
	t.lastScan, t.wantScan = now, false
	seen := map[string]bool{}
	for _, pat := range t.Patterns {
		paths := pyGlob(pat)
		if len(paths) == 0 && !t.warned[pat] {
			t.Logf("no file matches %s yet (waiting)", pat)
			t.warned[pat] = true
		}
		for _, p := range paths {
			if seen[p] {
				continue
			}
			seen[p] = true
			t.consider(p, pat, now, initial)
		}
	}
	t.reg.expire(t.open)
	if len(t.rotatedPaths) > 1024 {
		t.rotatedPaths = map[string]bool{}
	}
}

func (t *Tailer) consider(path, pat string, now time.Time, initial bool) {
	fi, err := os.Stat(path)
	if err != nil {
		return
	}
	if !fi.Mode().IsRegular() {
		if !t.bad[path] {
			t.Logf("cannot open %s: not a regular file", path)
			t.bad[path] = true
		}
		return
	}
	id, _ := idOf(fi)
	if h := t.byPath[path]; h != nil {
		if h.id == id {
			h.e.Seen = now.Unix()
			return
		}
		t.markRotated(h, now) // replaced since the last rotation check
	}
	if h := t.open[id]; h != nil { // already followed under another name (renamed within the globs)
		h.e.Seen = now.Unix()
		return
	}
	f, err := os.Open(path)
	if err != nil {
		if !t.bad[path] {
			t.Logf("cannot open %s: %v", path, err)
			t.bad[path] = true
		}
		return
	}
	delete(t.bad, path)
	fi2, err := f.Stat()
	if id2, _ := idOf(fi2); err != nil || id2 != id { // replaced between stat and open: next scan
		_ = f.Close()
		t.wantScan = true
		return
	}
	size := fi2.Size()
	e := t.reg.entries[id]
	if e != nil && e.FPN > 0 && (size < int64(e.FPN) || fingerprint(f, e.FPN) != e.FP) {
		delete(t.reg.entries, id) // the inode now belongs to another file
		e = nil
	}
	replaced := t.rotatedPaths[path] // the path's previous file was rotated away: this one is new
	delete(t.rotatedPaths, path)
	if initial && e == nil {
		for oid, oe := range t.reg.entries {
			if oe.Active && oe.Path == path && oid != id { // rotated while the agent was stopped
				replaced = true
				break
			}
		}
	}
	var off int64
	if e != nil {
		off = e.Off
		if size < off { // truncated while nobody followed it
			off = 0
		}
	} else {
		fp := ""
		if size >= fpMax && !replaced {
			fp = fingerprint(f, fpMax)
		}
		copyOff, isCopy := t.reg.copyStart(fp, size, t.open)
		switch {
		case isCopy: // a copy of content already followed (copytruncate into a name the globs match)
			off = copyOff
		case !replaced && initial && (!t.reg.hadState || !t.reg.prevPats[pat]): // first sight: from the end, like agent.py / tail -f
			off = size
		case size > t.BigNew:
			off = size
			t.Logf("%s appeared with %d MB: following it from its end", path, size>>20)
		default: // appeared since the last scan, or while the agent was stopped: from its start
			off = 0
		}
		e = &posEntry{Dev: id.Dev, Ino: id.Ino, Off: off}
		t.reg.entries[id] = e
	}
	e.Path, e.Seen, e.Off = path, now.Unix(), off
	if n := min(size, fpMax); int64(e.FPN) < n {
		e.FP, e.FPN = fingerprint(f, int(n)), int(n)
	}
	if _, err := f.Seek(off, io.SeekStart); err != nil {
		_ = f.Close()
		return
	}
	h := &harvester{id: id, key: path, path: path, name: filepath.Base(path), f: f, rd: off, pendEnd: off, lastGrow: now, e: e}
	t.open[id], t.byPath[path] = h, h
	t.listDirty, t.reg.dirty = true, true
	t.Logf("tailing %s", path)
}

// recoverRotated: a file that was the live file of its path when the agent stopped and is gone from it now was rotated
// meanwhile; when it still sits in the same directory under another name, the lines after the saved position are shipped
// (under the original name, as agent.py does for a rotated file's last lines).
func (t *Tailer) recoverRotated(now time.Time) {
	for id, e := range t.reg.entries {
		if !e.Active || t.open[id] != nil {
			continue
		}
		e.Active, t.reg.dirty = false, true
		ents, err := os.ReadDir(filepath.Dir(e.Path))
		if err != nil {
			continue
		}
		for _, de := range ents {
			if !de.Type().IsRegular() {
				continue
			}
			p := filepath.Join(filepath.Dir(e.Path), de.Name())
			if p == e.Path { // still there under its own name: no longer configured, not rotated
				continue
			}
			fi, err := os.Stat(p)
			if err != nil {
				continue
			}
			if fid, _ := idOf(fi); fid != id || fi.Size() <= e.Off {
				continue
			}
			f, err := os.Open(p)
			if err != nil {
				break
			}
			if e.FPN > 0 && fingerprint(f, e.FPN) != e.FP {
				_ = f.Close()
				break
			}
			if _, err := f.Seek(e.Off, io.SeekStart); err != nil {
				_ = f.Close()
				break
			}
			h := &harvester{id: id, key: e.Path, path: p, name: filepath.Base(e.Path), f: f, rd: e.Off, pendEnd: e.Off, rotated: true, lastGrow: now, e: e}
			t.open[id] = h
			t.listDirty = true
			t.Logf("%s was rotated while the agent was stopped: shipping the rest of %s", e.Path, p)
			break
		}
	}
}

func (t *Tailer) markRotated(h *harvester, now time.Time) {
	h.rotated, h.lastGrow = true, now
	if t.byPath[h.path] == h {
		delete(t.byPath, h.path)
		t.rotatedPaths[h.path] = true
	}
	t.wantScan = true
}

// poll reads what was appended to every file; true when a file still has unread bytes (its batch is full).
func (t *Tailer) poll(now time.Time, checks bool) bool {
	more := false
	for _, h := range t.harvesters() {
		if h.closing {
			continue
		}
		// a file quiet for 10 s is looked at once a second instead of every poll (most followed files are idle)
		if checks && !h.rotated && now.Sub(h.lastGrow) > 10*time.Second && now.Sub(h.lastCheck) < time.Second {
			continue
		}
		h.lastCheck = now
		fi, err := h.f.Stat()
		if err != nil {
			continue
		}
		size := fi.Size()
		if size < h.rd { // truncated in place (copytruncate): start over
			if h.e.FPN == fpMax { // a copy of the old content (copytruncate) continues where this one was shipped
				t.reg.ghosts[h.e.FP] = ghost{FP: h.e.FP, Off: h.pendEnd, Exp: now.Add(time.Hour).Unix()}
			}
			h.e.FP, h.e.FPN = "", 0
			if _, err := h.f.Seek(0, io.SeekStart); err == nil {
				h.rd, h.partial = 0, h.partial[:0]
				h.pendEnd = 0
				t.reg.dirty = true
			}
		}
		if size > h.rd {
			if len(h.pend) < t.MaxBatch {
				t.read(h)
				h.lastGrow = now
			}
			if h.rd < size {
				more = true
			}
		}
		if h.e.FPN < fpMax && h.rd > int64(h.e.FPN) {
			n := min(h.rd, fpMax)
			h.e.FP, h.e.FPN = fingerprint(h.f, int(n)), int(n)
		}
		if checks && !h.rotated && now.Sub(h.lastRot) >= t.RotateCheck {
			h.lastRot = now
			st, err := os.Stat(h.path)
			if id, _ := idOfErr(st, err); err != nil || id != h.id { // renamed / deleted: drain the old file, reopen the path
				t.markRotated(h, now)
			}
		}
		if checks && h.rotated && now.Sub(h.lastGrow) >= t.RotatedGrace && h.rd >= size {
			if len(h.partial) > 0 { // a fragment left in the rotated file is its last line (agent.py)
				h.pend = fragmentLine(h.pend, h.partial)
				h.partial = h.partial[:0]
				h.pendEnd = h.rd
			}
			h.closing = true
		}
	}
	return more
}

func idOfErr(fi os.FileInfo, err error) (fileID, bool) {
	if err != nil || fi == nil {
		return fileID{}, false
	}
	return idOf(fi)
}

func (t *Tailer) read(h *harvester) {
	for len(h.pend) < t.MaxBatch {
		want := min(len(t.buf), max(t.MaxBatch-len(h.pend), 512)) // batches stay near MaxBatch
		n, err := h.f.Read(t.buf[:want])
		if n > 0 {
			h.rd += int64(n)
			t.consume(h, t.buf[:n])
		}
		if err != nil || n < want {
			return
		}
	}
}

func (t *Tailer) consume(h *harvester, chunk []byte) {
	data := chunk
	aliased := len(h.partial) > 0
	if aliased {
		h.partial = append(h.partial, chunk...)
		data = h.partial
	}
	var used int
	h.pend, used = splitLines(h.pend, data)
	rest := data[used:]
	if len(rest) > t.MaxLine { // a line without end beyond the cap: shipped as it is, memory stays bounded
		h.pend = fragmentLine(h.pend, rest)
		rest = rest[:0]
	}
	if aliased {
		h.partial = h.partial[:copy(h.partial, rest)]
		if cap(h.partial) > 256<<10 && len(h.partial) < 4<<10 {
			h.partial = append([]byte(nil), h.partial...)
		}
	} else {
		h.partial = append(h.partial[:0], rest...)
	}
	h.pendEnd = h.rd - int64(len(h.partial))
}

func (t *Tailer) flushDue(now time.Time) bool {
	due := now.Sub(t.lastFlush) >= t.FlushEvery
	for _, h := range t.harvesters() {
		if h.closing || len(h.pend) >= t.MaxBatch || (due && len(h.pend) > 0) || (len(h.pend) == 0 && h.e.Off != h.pendEnd) {
			return true
		}
	}
	return false
}

// flush sends every file's pending lines (a few files in parallel), then advances and saves the positions of the batches
// that were delivered, spooled or refused for good. A Retry (no spool, receiver unreachable) keeps the lines for the next
// try; flush then reports true (held) so the caller waits before reading on.
func (t *Tailer) flush(ctx context.Context) (held bool) {
	t.lastFlush = time.Now()
	var jobs []*harvester
	for _, h := range t.harvesters() {
		if len(h.pend) > 0 || h.closing || h.e.Off != h.pendEnd {
			jobs = append(jobs, h)
		}
	}
	if len(jobs) == 0 {
		return false
	}
	// one batch per path, as agent.py: a rotated file's last lines and the new file's first ones go out together, in order
	groups := map[string][]int{}
	var keys []string
	for i, h := range jobs {
		if groups[h.key] == nil {
			keys = append(keys, h.key)
		}
		groups[h.key] = append(groups[h.key], i)
	}
	res := make([]Result, len(jobs))
	var wg sync.WaitGroup
	sem := make(chan struct{}, t.Parallel)
	for _, k := range keys {
		idx := groups[k]
		sort.SliceStable(idx, func(a, b int) bool { return jobs[idx[a]].rotated && !jobs[idx[b]].rotated })
		body := jobs[idx[0]].pend
		if len(idx) > 1 {
			body = nil
			for _, i := range idx {
				body = append(body, jobs[i].pend...)
			}
		}
		if len(body) == 0 {
			continue
		}
		wg.Add(1)
		go func(idx []int, body []byte, name string) {
			defer wg.Done()
			sem <- struct{}{}
			r := t.Send(ctx, body, name)
			<-sem
			for _, i := range idx {
				res[i] = r
			}
		}(idx, body, jobs[idx[0]].name)
	}
	wg.Wait()
	for i, h := range jobs {
		if res[i] == Retry {
			held = true
			continue
		}
		h.pend = h.pend[:0]
		if cap(h.pend) > 4*t.MaxBatch {
			h.pend = nil
		}
		if h.e.Off != h.pendEnd {
			h.e.Off = h.pendEnd
			t.reg.dirty = true
		}
		if h.closing {
			_ = h.f.Close()
			delete(t.open, h.id)
			if t.byPath[h.path] == h {
				delete(t.byPath, h.path)
			}
			t.listDirty, t.reg.dirty = true, true
		}
	}
	if t.reg.dirty {
		t.saveLog()
	}
	return held
}
