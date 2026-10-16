package ship

import (
	"bytes"
	"unicode/utf8"
)

// The text rules of agent.py's tail: it opens files with open(path, "r", encoding="utf-8", errors="replace"), i.e. universal
// newlines ("\r\n" and a lone "\r" become "\n") and one U+FFFD per maximal ill-formed subsequence. wo-agent applies the same
// rules to the raw bytes so the receiver sees the same body; file offsets stay raw.

// maximalSubpart is the length of the ill-formed sequence at the start of s that Python (and the Unicode standard's
// "maximal subpart" practice) replaces with a single U+FFFD. s[0] is not the start of a valid sequence.
func maximalSubpart[T ~string | ~[]byte](s T) int {
	c := s[0]
	var need int
	lo, hi := byte(0x80), byte(0xbf)
	switch {
	case c >= 0xc2 && c <= 0xdf:
		need = 1
	case c == 0xe0:
		need, lo = 2, 0xa0
	case c >= 0xe1 && c <= 0xec, c == 0xee, c == 0xef:
		need = 2
	case c == 0xed:
		need, hi = 2, 0x9f
	case c == 0xf0:
		need, lo = 3, 0x90
	case c >= 0xf1 && c <= 0xf3:
		need = 3
	case c == 0xf4:
		need, hi = 3, 0x8f
	default:
		return 1
	}
	n := 1
	for k := 0; k < need && n < len(s); k++ {
		b := s[n]
		if b < lo || b > hi {
			break
		}
		n++
		lo, hi = 0x80, 0xbf
	}
	return n
}

// appendText appends s with invalid UTF-8 replaced like Python's errors="replace".
func appendText(dst, s []byte) []byte {
	if utf8.Valid(s) {
		return append(dst, s...)
	}
	for i := 0; i < len(s); {
		c := s[i]
		if c < utf8.RuneSelf {
			dst = append(dst, c)
			i++
			continue
		}
		r, size := utf8.DecodeRune(s[i:])
		if r == utf8.RuneError && size == 1 {
			dst = append(dst, "�"...)
			i += maximalSubpart(s[i:])
			continue
		}
		dst = append(dst, s[i:i+size]...)
		i += size
	}
	return dst
}

// splitLines appends every complete line of data to out, normalised ("\n" endings, valid UTF-8), and returns how many raw
// bytes it consumed. A trailing fragment without a line end stays unconsumed, and so does a final "\r" (it may be the first
// half of "\r\n": Python's newline decoder waits for the next byte too).
func splitLines(out, data []byte) ([]byte, int) {
	if bytes.IndexByte(data, '\r') < 0 { // fast path: plain "\n" lines
		last := bytes.LastIndexByte(data, '\n')
		if last < 0 {
			return out, 0
		}
		return appendText(out, data[:last+1]), last + 1
	}
	start := 0
	for i := 0; i < len(data); i++ {
		switch data[i] {
		case '\n':
			out = appendText(out, data[start:i])
			out = append(out, '\n')
			start = i + 1
		case '\r':
			if i+1 == len(data) {
				return out, start
			}
			out = appendText(out, data[start:i])
			out = append(out, '\n')
			if data[i+1] == '\n' {
				i++
			}
			start = i + 1
		}
	}
	return out, start
}

// fragmentLine ships a trailing fragment (no line end inside, at most a final "\r") as a line of its own: agent.py does
// this when the file was rotated away; wo-agent also when a line outgrows the line cap.
func fragmentLine(out, frag []byte) []byte {
	return append(appendText(out, bytes.TrimRight(frag, "\r")), '\n')
}
