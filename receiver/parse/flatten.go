package parse

import "watchover/receiver/parse/py"

// Owner: WP2 (P02 section 11).

// Flatten is normalize.flatten(d, prefix): nested dicts become dotted keys (later writes overwrite in place, empty
// dicts vanish, lists are kept as values).
func Flatten(d *py.Dict, prefix string) *py.Dict {
	out := py.NewDict(0)
	flattenInto(out, d, prefix)
	return out
}

func flattenInto(out, d *py.Dict, prefix string) {
	if d == nil {
		return
	}
	d.Range(func(k string, v py.Value) bool {
		if v.K == py.KMap {
			flattenInto(out, v.M, prefix+k+".")
		} else {
			out.Set(prefix+k, v)
		}
		return true
	})
}
