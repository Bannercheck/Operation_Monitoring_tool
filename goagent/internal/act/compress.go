package act

import (
	"compress/gzip"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

// compress_older_than and its rollback decompress_list (§5.7, R8). Manifest first: the full file list is written to
// state/compress-<job_id>.json and fsynced (file and directory) before the first file is touched; when the state file system
// is full, reserve.bin is deleted to make room; if the manifest still cannot be written durably the act is refused
// (manifest_failed, nothing changed). Each finished file is appended to the manifest's journal. The rollback restores the
// files of one manifest, or — when the manifest is missing — the *.gz files under the template root whose ctime is at or after
// the job's start and whose original name does not exist (ctime cannot be set back, so that set is exactly what the job made).

const reserveSize = 4 << 20

// hooks for tests: called right before the first file changes; the manifest writer (full-disk simulation).
var (
	beforeFirstChange = func() {}
	writeDurable      = writeFileDurable
)

type manifestFile struct {
	Path  string `json:"path"`
	Size  int64  `json:"size"`
	Mode  uint32 `json:"mode"`
	Mtime int64  `json:"mtime"`
	UID   int    `json:"uid"`
	GID   int    `json:"gid"`
}

type manifest struct {
	JobID   string         `json:"job_id"`
	Started string         `json:"started"`
	Root    string         `json:"root"`
	Days    int            `json:"days"`
	Files   []manifestFile `json:"files"`
}

func writeFileDurable(path string, data []byte) error {
	tmp := path + ".tmp"
	f, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o600)
	if err != nil {
		return err
	}
	if _, err = f.Write(data); err == nil {
		err = f.Sync()
	}
	if cerr := f.Close(); err == nil {
		err = cerr
	}
	if err != nil {
		os.Remove(tmp)
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		os.Remove(tmp)
		return err
	}
	return syncDir(filepath.Dir(path))
}

func syncDir(d string) error {
	f, err := os.Open(d)
	if err != nil {
		return err
	}
	defer f.Close()
	return f.Sync()
}

// EnsureReserve pre-allocates state/reserve.bin (4 MB) so a manifest still fits on a full state file system.
func EnsureReserve(stateDir string) error {
	p := filepath.Join(stateDir, "reserve.bin")
	if fi, err := os.Stat(p); err == nil && fi.Size() >= reserveSize {
		return nil
	}
	f, err := os.OpenFile(p, os.O_WRONLY|os.O_CREATE, 0o600)
	if err != nil {
		return err
	}
	defer f.Close()
	if _, err := f.Write(make([]byte, reserveSize)); err != nil {
		os.Remove(p)
		return err
	}
	return f.Sync()
}

func isNoSpace(err error) bool {
	return errors.Is(err, syscall.ENOSPC) || errors.Is(err, syscall.EDQUOT)
}

// rotatedLog matches find -name '*.log.*' ! -name '*.gz'.
func rotatedLog(name string) bool {
	i := strings.Index(name, ".log.")
	return i > 0 && len(name) > i+5 && !strings.HasSuffix(name, ".gz")
}

func biCompress(c *BuiltinCall) (BuiltinOut, error) {
	a := c.A
	logical := str(c.Vals["path"])
	days := intVal(c.Vals["days"], 7)
	root := a.Env.Real(logical)
	cutoff := c.Start.Add(-time.Duration(days) * 24 * time.Hour)
	open := OpenFiles()
	var files []manifestFile
	var before int64
	err := walk(c.Ctx, root, func(p string, fi fs.FileInfo, d int) {
		if !rotatedLog(fi.Name()) || !fi.ModTime().Before(cutoff) || open[p] {
			return
		}
		st := fi.Sys().(*syscall.Stat_t)
		files = append(files, manifestFile{Path: a.Env.Logical(p), Size: fi.Size(), Mode: uint32(fi.Mode().Perm()), Mtime: fi.ModTime().UnixNano(),
			UID: int(st.Uid), GID: int(st.Gid)})
		before += fi.Size()
	})
	if err != nil {
		return BuiltinOut{}, err
	}
	if c.Dry {
		// "bytes" is what runbooks' dry_expect reads (§6.1: files <= 10000 and bytes >= 104857600)
		return BuiltinOut{Fields: map[string]any{"files": len(files), "bytes": before, "bytes_before": before}}, nil
	}
	m := manifest{JobID: c.Job.ID, Started: c.Start.UTC().Format(time.RFC3339Nano), Root: logical, Days: days, Files: files}
	data := MustCanonical(m)
	mpath := filepath.Join(a.Cfg.StateDir, "compress-"+c.Job.ID+".json")
	if err := writeDurable(mpath, data); err != nil {
		if !isNoSpace(err) {
			return BuiltinOut{Reject: "manifest_failed"}, nil
		}
		os.Remove(filepath.Join(a.Cfg.StateDir, "reserve.bin")) // make room, then try once more
		_ = syncDir(a.Cfg.StateDir)
		if err := writeDurable(mpath, data); err != nil {
			return BuiltinOut{Reject: "manifest_failed"}, nil
		}
	}
	journal, err := os.OpenFile(filepath.Join(a.Cfg.StateDir, "compress-"+c.Job.ID+".journal"), os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o600)
	if err != nil {
		return BuiltinOut{Reject: "manifest_failed"}, nil
	}
	defer journal.Close()
	beforeFirstChange()
	var after int64
	done := 0
	var firstErr error
	for _, f := range files {
		if c.Ctx.Err() != nil {
			firstErr = c.Ctx.Err()
			break
		}
		n, err := gzipFile(a.Env.Real(f.Path), f)
		if err != nil {
			firstErr = err
			break
		}
		after += n
		done++
		_, _ = journal.WriteString(f.Path + "\n")
		_ = journal.Sync()
	}
	_ = EnsureReserve(a.Cfg.StateDir)
	fields := map[string]any{"list_id": c.Job.ID, "files": done, "bytes_before": before, "bytes_after": after}
	if firstErr != nil {
		return BuiltinOut{Fields: fields}, fmt.Errorf("stopped after %d of %d files: %v", done, len(files), firstErr)
	}
	return BuiltinOut{Fields: fields}, nil
}

// gzipFile writes <p>.gz (level 9, original mtime, mode and owner), fsyncs it and removes <p>; the compressed size.
func gzipFile(p string, mf manifestFile) (int64, error) {
	src, err := os.Open(p)
	if err != nil {
		return 0, err
	}
	defer src.Close()
	dst := p + ".gz"
	out, err := os.OpenFile(dst, os.O_WRONLY|os.O_CREATE|os.O_EXCL, os.FileMode(mf.Mode))
	if err != nil {
		return 0, err
	}
	zw, _ := gzip.NewWriterLevel(out, gzip.BestCompression)
	zw.Name = filepath.Base(p)
	zw.ModTime = time.Unix(0, mf.Mtime)
	_, err = io.Copy(zw, src)
	if err == nil {
		err = zw.Close()
	}
	if err == nil {
		err = out.Sync()
	}
	if cerr := out.Close(); err == nil {
		err = cerr
	}
	if err != nil {
		os.Remove(dst)
		return 0, err
	}
	restoreMeta(dst, mf)
	if err := os.Remove(p); err != nil {
		return 0, err
	}
	_ = syncDir(filepath.Dir(p))
	fi, _ := os.Stat(dst)
	if fi == nil {
		return 0, nil
	}
	return fi.Size(), nil
}

func restoreMeta(p string, mf manifestFile) {
	if os.Geteuid() == 0 {
		_ = os.Lchown(p, mf.UID, mf.GID)
	}
	_ = os.Chmod(p, os.FileMode(mf.Mode))
	t := time.Unix(0, mf.Mtime)
	_ = os.Chtimes(p, t, t)
}

// gunzipFile restores <orig> from <orig>.gz and removes the .gz; the restored size.
func gunzipFile(orig string, mf manifestFile) (int64, error) {
	gz := orig + ".gz"
	in, err := os.Open(gz)
	if err != nil {
		return 0, err
	}
	defer in.Close()
	zr, err := gzip.NewReader(in)
	if err != nil {
		return 0, err
	}
	if mf.Mode == 0 {
		if fi, err := in.Stat(); err == nil {
			mf.Mode = uint32(fi.Mode().Perm())
			st := fi.Sys().(*syscall.Stat_t)
			mf.UID, mf.GID = int(st.Uid), int(st.Gid)
		}
		if !zr.ModTime.IsZero() {
			mf.Mtime = zr.ModTime.UnixNano()
		}
	}
	out, err := os.OpenFile(orig, os.O_WRONLY|os.O_CREATE|os.O_EXCL, os.FileMode(mf.Mode))
	if err != nil {
		return 0, err
	}
	n, err := io.Copy(out, zr)
	if err == nil {
		err = out.Sync()
	}
	if cerr := out.Close(); err == nil {
		err = cerr
	}
	if err != nil {
		os.Remove(orig)
		return 0, err
	}
	if mf.Mtime != 0 {
		restoreMeta(orig, mf)
	}
	if err := os.Remove(gz); err != nil {
		return n, err
	}
	_ = syncDir(filepath.Dir(orig))
	return n, nil
}

func biDecompress(c *BuiltinCall) (BuiltinOut, error) {
	a := c.A
	listID := str(c.Vals["list_id"])
	var m manifest
	raw, err := os.ReadFile(filepath.Join(a.Cfg.StateDir, "compress-"+listID+".json"))
	fromManifest := err == nil && Decode(raw, &m) == nil && m.JobID == listID
	var targets []manifestFile
	if fromManifest {
		targets = m.Files
	} else {
		// fallback: *.gz under the template's write roots, ctime >= the job's start, original name free
		started, ok := a.Ledger.Started(listID)
		if !ok {
			return BuiltinOut{}, fmt.Errorf("no manifest and no ledger line for %s", listID)
		}
		for _, r := range c.Tmpl.Strings("writes") {
			_ = walk(c.Ctx, a.Env.Real(r), func(p string, fi fs.FileInfo, d int) {
				if !strings.HasSuffix(p, ".gz") {
					return
				}
				st := fi.Sys().(*syscall.Stat_t)
				ctime := time.Unix(int64(st.Ctim.Sec), int64(st.Ctim.Nsec))
				orig := strings.TrimSuffix(p, ".gz")
				if ctime.Before(started) {
					return
				}
				if _, err := os.Lstat(orig); err == nil {
					return
				}
				targets = append(targets, manifestFile{Path: a.Env.Logical(orig)})
			})
		}
	}
	var gzBytes, restored int64
	n := 0
	var firstErr error
	for _, f := range targets {
		orig := a.Env.Real(f.Path)
		gi, err := os.Stat(orig + ".gz")
		if err != nil {
			continue // not compressed (the act stopped early) or already restored
		}
		if _, err := os.Lstat(orig); err == nil {
			continue
		}
		size, err := gunzipFile(orig, f)
		if err != nil {
			firstErr = err
			break
		}
		gzBytes += gi.Size()
		restored += size
		n++
	}
	fields := map[string]any{"list_id": listID, "files": n, "bytes_before": gzBytes, "bytes_after": restored}
	if !fromManifest {
		fields["fallback"] = "ctime"
	}
	if firstErr != nil {
		return BuiltinOut{Fields: fields}, firstErr
	}
	return BuiltinOut{Fields: fields}, nil
}
