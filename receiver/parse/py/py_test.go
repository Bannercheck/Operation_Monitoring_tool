package py

import (
	"errors"
	"math"
	"reflect"
	"regexp"
	"testing"
	"unicode"
)

func TestSplitLines(t *testing.T) {
	cases := []struct {
		in   string
		want []string
	}{
		{"", nil},
		{"\n", []string{""}},
		{"a\n\nb", []string{"a", "", "b"}},
		{"a\r\nb\rc\n", []string{"a", "b", "c"}},
		{"a\x0bb\x0cc\x1cd\x1de\x1ef", []string{"a", "b", "c", "d", "e", "f"}},
		{"a\x1fb", []string{"a\x1fb"}}, // \x1f is whitespace but not a line boundary
		{"a\u0085b c d", []string{"a", "b", "c", "d"}},
		{"a\r", []string{"a"}},
		{"\r\n\r\n", []string{"", ""}},
		{"x\n\r", []string{"x", ""}},
	}
	for _, c := range cases {
		if got := SplitLines(c.in); !reflect.DeepEqual(got, c.want) {
			t.Errorf("SplitLines(%q) = %q, want %q", c.in, got, c.want)
		}
	}
	if got := SplitLinesN("a\nb\nc\nd", 2); !reflect.DeepEqual(got, []string{"a", "b"}) {
		t.Errorf("SplitLinesN = %q", got)
	}
	if got := SplitLinesN("a\nb", 5); !reflect.DeepEqual(got, []string{"a", "b"}) {
		t.Errorf("SplitLinesN = %q", got)
	}
}

func TestLowerUpper(t *testing.T) {
	cases := map[string]string{
		"İNVENTORY": "i̇nventory",
		"ΟΔΟΣ":      "οδος",
		"ΟΔΟΣ ΑΒ":   "οδος αβ",
		"Σ":         "σ",
		"AΣB":       "aσb",
		"ΑΣ.":       "ας.",
		"ı":         "ı",
		"ABC":       "abc",
		"K":         "k",
	}
	for in, want := range cases {
		if got := Lower(in); got != want {
			t.Errorf("Lower(%q) = %q, want %q", in, got, want)
		}
	}
	if got := Upper("straße ı"); got != "STRASSE I" {
		t.Errorf("Upper = %q", got)
	}
}

func TestStripSplit(t *testing.T) {
	if got := Strip("\x1c\x1d a b 　\x1f"); got != "a b" {
		t.Errorf("Strip = %q", got)
	}
	if got := LStrip("\x1c x "); got != "x " {
		t.Errorf("LStrip = %q", got)
	}
	if got := RStrip(" x \x1f"); got != " x" {
		t.Errorf("RStrip = %q", got)
	}
	if got := Strip("\ufeffx"); got != "\ufeffx" { // U+FEFF is not whitespace
		t.Errorf("Strip BOM = %q", got)
	}
	if got := Split(" a\x1c b c "); !reflect.DeepEqual(got, []string{"a", "b", "c"}) {
		t.Errorf("Split = %q", got)
	}
	if got := StripChars("--:a: -", " :-"); got != "a" {
		t.Errorf("StripChars = %q", got)
	}
	n := 0
	for r := rune(0); r < 0x110000; r++ {
		if IsSpace(r) {
			n++
		}
	}
	if n != 29 {
		t.Errorf("IsSpace set has %d code points, want 29", n)
	}
	re := regexp.MustCompile(`^` + PS + `+$`)
	if !re.MatchString(WS) {
		t.Errorf("PS does not match all of WS")
	}
	if Len(WS) != 29 {
		t.Errorf("len(WS) = %d", Len(WS))
	}
}

func TestDigits(t *testing.T) {
	if !IsDigitStr("²") || IsDecimalStr("²") {
		t.Error("superscript two: isdigit true, isdecimal false")
	}
	if !IsDigitStr("١٢") || !IsDecimalStr("١٢") || IsDigitStr("") || IsDigitStr("1a") {
		t.Error("isdigit basics")
	}
	if !IsDigitStr("①") {
		t.Error("circled digit one is a digit")
	}
	if DigitValue('٧') != 7 || DigitValue('𝟗') != 9 || DigitValue('x') != -1 || DigitValue('𝟎') != 0 || DigitValue('𝟘') != 0 {
		t.Error("DigitValue")
	}
	if ToASCIIDigits("١٢.٥ms") != "12.5ms" {
		t.Error("ToASCIIDigits")
	}
}

func TestAtoiParseFloat(t *testing.T) {
	okI := map[string]string{" 12 ": "12", "-0": "0", "+7": "7", "1_000": "1000", "١٧٩٠٠٦٨٤٤٢": "1790068442",
		"99999999999999999999999": "99999999999999999999999"}
	for in, want := range okI {
		n, err := Atoi(in)
		if err != nil || n.String() != want {
			t.Errorf("Atoi(%q) = %v, %v", in, n, err)
		}
	}
	for _, in := range []string{"", "_1", "1_", "1__0", "²", "1.0", "0x10", "- 1"} {
		if _, err := Atoi(in); !errors.Is(err, ErrValue) {
			t.Errorf("Atoi(%q) err = %v", in, err)
		}
	}
	long := make([]byte, 4301)
	for i := range long {
		long[i] = '1'
	}
	if _, err := Atoi(string(long)); err == nil {
		t.Error("Atoi 4301 digits must fail")
	}
	okF := map[string]float64{"1.": 1, ".5": 0.5, " 1_000.5 ": 1000.5, "1e1_0": 1e10, "١٢.٥": 12.5, "-0": math.Copysign(0, -1),
		"1E5": 1e5, "  -InFiNiTy": math.Inf(-1), "inf": math.Inf(1), "1e400": math.Inf(1)}
	for in, want := range okF {
		f, err := ParseFloat(in)
		if err != nil || f != want || math.Signbit(f) != math.Signbit(want) {
			t.Errorf("ParseFloat(%q) = %v, %v want %v", in, f, err, want)
		}
	}
	if f, err := ParseFloat("nan"); err != nil || !math.IsNaN(f) {
		t.Error("ParseFloat nan")
	}
	for _, in := range []string{"", ".", "e5", "1e", "0x10", "1_.5", "1._5", "_1", "1__0", "infx", "Inf1", "1.5e+"} {
		if _, err := ParseFloat(in); err == nil {
			t.Errorf("ParseFloat(%q) must fail", in)
		}
	}
}

func TestRound(t *testing.T) {
	cases := []struct {
		x    float64
		n    int
		want float64
	}{{0.605, 2, 0.6}, {0.615, 2, 0.61}, {0.625, 2, 0.62}, {0.125, 2, 0.12}, {0.95, 4, 0.95}, {1.2, 4, 1.2},
		{1.2345678, 4, 1.2346}, {2.0 / 3, 2, 0.67}, {0.6066, 2, 0.61}}
	for _, c := range cases {
		if got := Round(c.x, c.n); got != c.want {
			t.Errorf("Round(%v, %d) = %v want %v", c.x, c.n, got, c.want)
		}
	}
}

func TestSliceHead(t *testing.T) {
	s := "aé€𝟘b"
	if Head(s, 3) != "aé€" || Head(s, 10) != s || Head(s, 0) != "" {
		t.Error("Head")
	}
	if Slice(s, 1, 3) != "é€" || Slice(s, -2, 5) != "𝟘b" || Slice(s, 3, 100) != "𝟘b" || Slice(s, 4, 2) != "" || Slice(s, -100, 1) != "a" {
		t.Error("Slice")
	}
}

func TestFold(t *testing.T) {
	same := [][2]rune{{'i', 'I'}, {'i', 'ı'}, {'I', 'İ'}, {'ı', 'İ'}, {'s', 'ſ'}, {'S', 'ſ'}, {'k', 'K'}, {'K', 'K'},
		{'σ', 'ς'}, {'Σ', 'ς'}, {'µ', 'μ'}}
	for _, p := range same {
		if !FoldEqual(p[0], p[1]) || !FoldEqual(p[1], p[0]) {
			t.Errorf("FoldEqual(%q, %q) = false", p[0], p[1])
		}
	}
	for _, p := range [][2]rune{{'i', 'j'}, {'a', 'á'}, {'s', 'ş'}} {
		if FoldEqual(p[0], p[1]) {
			t.Errorf("FoldEqual(%q, %q) = true", p[0], p[1])
		}
	}
	if n, ok := HasFoldPrefix("ınfo x", "INFO"); !ok || n != 5 {
		t.Errorf("HasFoldPrefix ınfo = %d %v", n, ok)
	}
	if _, ok := HasFoldPrefix("INF", "INFO"); ok {
		t.Error("HasFoldPrefix short")
	}
}

func TestIsWord(t *testing.T) {
	for _, r := range []rune{'a', 'Z', '0', '_', 'é', 'ı', '٣', '²', 'Ⅻ', '中'} {
		if !IsWord(r) {
			t.Errorf("IsWord(%q) false", r)
		}
	}
	for _, r := range []rune{' ', '-', '.', ' ', '́', '€'} {
		if IsWord(r) {
			t.Errorf("IsWord(%q) true", r)
		}
	}
	w := regexp.MustCompile(`^` + W + `$`)
	for r := rune(0); r < 0x3000; r++ {
		if w.MatchString(string(r)) != IsWord(r) {
			t.Fatalf("W class and IsWord disagree on %U", r)
		}
	}
}

func TestTranslate(t *testing.T) {
	cases := map[string]string{
		`\d+`:           `\p{Nd}+`,
		`[\w.\-@]+`:     `[\p{L}\p{N}_.\-@]+`,
		`a\sb\S`:        `a` + PS + `b` + PNS,
		`[^\s:]`:        `[^` + wsClass + `:]`,
		`\W`:            NW,
		`[]a]`:          `[\]a]`,
		"\x5cu00e9":     `\x{00e9}`,
		"\x5cU0001F600": `\x{0001F600}`,
		`(?P<x>\d)`:     `(?P<x>\p{Nd})`,
		`[\b]`:          `[\x08]`,
		`a+?b*?\++`:     `a+?b*?\++`,
		`[[]`:           `[\[]`,
	}
	for in, want := range cases {
		if got := Translate(in); got != want {
			t.Errorf("Translate(%q) = %q, want %q", in, got, want)
		}
		regexp.MustCompile(Translate(in))
	}
	if TranslateKeepB(`\b\d+\b`) != `\b\p{Nd}+\b` {
		t.Error("TranslateKeepB")
	}
}

func TestTranslateRejects(t *testing.T) {
	for _, p := range []string{`a(?=b)`, `a(?!b)`, `(?<=a)b`, `(?<!a)b`, `\bword`, `abc\Z`, `(?P<q>a)(?P=q)`, `a*+`, `a++`,
		`(?>ab)`, `(a)\1`, `[\S]`, `[\W]`, `\B`, `a?+`} {
		func() {
			defer func() {
				if recover() == nil {
					t.Errorf("Translate(%q) did not panic", p)
				}
			}()
			Translate(p)
		}()
	}
}

func TestFloatRepr(t *testing.T) {
	cases := map[float64]string{1.0: "1.0", 1e-05: "1e-05", 1e16: "1e+16", 1.2345678901234567e+19: "1.2345678901234567e+19",
		0.0001: "0.0001", 100000.0: "100000.0", 245.12: "245.12", 1234567890123456.0: "1234567890123456.0", 0.95: "0.95",
		1.23: "1.23", 3e-07: "3e-07", 1.5e-05: "1.5e-05", 1e22: "1e+22", 123.0: "123.0", 5e-324: "5e-324"}
	for f, want := range cases {
		if got := FloatRepr(f); got != want {
			t.Errorf("FloatRepr(%v) = %q, want %q", f, got, want)
		}
	}
	if FloatRepr(math.Copysign(0, -1)) != "-0.0" || FloatRepr(math.NaN()) != "NaN" || FloatRepr(math.Inf(-1)) != "-Infinity" {
		t.Error("FloatRepr specials")
	}
	if FloatStr(math.Inf(1)) != "inf" || FloatStr(math.NaN()) != "nan" || FloatStr(2) != "2.0" {
		t.Error("FloatStr")
	}
}

func TestLoads(t *testing.T) {
	v, err := Loads(`{"a": 1, "b": 2, "a": 3}`)
	if err != nil || v.M.Len() != 2 || v.M.Keys()[0] != "a" {
		t.Fatalf("dup keys: %v %v", v, err)
	}
	if a, _ := v.M.Get("a"); a.K != KInt || a.I != 3 {
		t.Errorf("dup key value = %v", a)
	}
	v, err = Loads(`[NaN, Infinity, -Infinity, 1e400, -0, -0.0, 1E5, 12345678901234567890123, 2, 2.0]`)
	if err != nil {
		t.Fatal(err)
	}
	l := v.L
	if !math.IsNaN(l[0].F) || !math.IsInf(l[1].F, 1) || !math.IsInf(l[2].F, -1) || !math.IsInf(l[3].F, 1) {
		t.Error("specials")
	}
	if l[4].K != KInt || l[4].I != 0 || l[5].K != KFloat || !math.Signbit(l[5].F) || l[6].K != KFloat || l[6].F != 1e5 {
		t.Error("zero / exponent")
	}
	if l[7].K != KInt || l[7].Big == nil || l[7].IntString() != "12345678901234567890123" {
		t.Error("big int")
	}
	if l[8].K != KInt || l[9].K != KFloat {
		t.Error("int vs float")
	}
	if s, _ := Dumps(v, true); s != `[NaN, Infinity, -Infinity, Infinity, 0, -0.0, 100000.0, 12345678901234567890123, 2, 2.0]` {
		t.Errorf("Dumps = %s", s)
	}
	bs := "\x5c" // a backslash, spelled so no tool rewrites the JSON escapes below
	v, err = Loads(`"` + bs + `ud800x` + bs + `ud83d` + bs + `ude00` + bs + `ud83dA` + bs + `u00e9"`)
	if err != nil || v.S != "\xed\xa0\x80x\U0001F600\xed\xa0\xbdA\u00e9" {
		t.Errorf("surrogates: %q %v", v.S, err)
	}
	bad := []string{"", " ", "\ufeff{}", "{,}", "[1,]", `{"a" 1}`, "01", "1.", "1e", "-", "nan", "\"a\x01\"", `"\x"`, `"\u12"`,
		" {}", "\x0c{}", "[1] x", "tru", `"abc`, `{"a":1,}`, "+1", ".5", "-Infinityx"}
	for _, s := range bad {
		if _, err := Loads(s); !errors.Is(err, ErrSyntax) {
			t.Errorf("Loads(%q) err = %v", s, err)
		}
	}
	good := []string{" \t\n\r{} ", "[]", "-1.5e-3", `"\/\b\f\n\r\t\"\\"`, "[[[]]]", `{"":null}`}
	for _, s := range good {
		if !Valid(s) {
			t.Errorf("Valid(%q) false", s)
		}
	}
	long := "1"
	for len(long) < 4301 {
		long += "1"
	}
	if _, err := Loads(long); !errors.Is(err, ErrRaise) {
		t.Errorf("4301 digits err = %v", err)
	}
	if _, err := Validate("[" + long + "]"); !errors.Is(err, ErrRaise) {
		t.Errorf("Validate 4301 digits err = %v", err)
	}
	deep := ""
	for i := 0; i < MaxDepth+1; i++ {
		deep += "["
	}
	if _, err := Loads(deep); !errors.Is(err, ErrDepth) {
		t.Errorf("depth err = %v", err)
	}
	if k, err := Validate(`{"a": [1, {"b": "c"}]}`); err != nil || k != KMap {
		t.Errorf("Validate kind = %v %v", k, err)
	}
}

func TestAppendString(t *testing.T) {
	got := string(AppendString(nil, "a\"b\\c\n\r\t\b\f\x01\x1f\x7f <>&    é 😀"))
	want := `"a\"b\\c\n\r\t\b\f\u0001\u001f` + "\x7f <>&    é 😀\""
	if got != want {
		t.Errorf("AppendString = %s, want %s", got, want)
	}
}

func TestDumpsOrder(t *testing.T) {
	d := NewDict(0)
	d.Set("b", S("x"))
	d.Set("a", Int(1))
	d.Set("é", B(true))
	d.Set("Z", Null)
	d.Set("f", F(2))
	if s, _ := Dumps(MapOf(d), false); s != `{"b": "x", "a": 1, "é": true, "Z": null, "f": 2.0}` {
		t.Errorf("insertion order: %s", s)
	}
	if s, _ := Dumps(MapOf(d), true); s != `{"Z": null, "a": 1, "b": "x", "f": 2.0, "é": true}` {
		t.Errorf("sorted: %s", s)
	}
	if _, err := AppendValue(nil, F(math.NaN()), true, false); !errors.Is(err, ErrNaN) {
		t.Error("ErrNaN")
	}
}

func TestDictSemantics(t *testing.T) {
	d := NewDict(0)
	for i := 0; i < 20; i++ {
		d.Set(string(rune('a'+i)), Int(int64(i)))
	}
	d.Set("c", S("x"))
	if d.Keys()[2] != "c" {
		t.Error("Set keeps position")
	}
	if v, ok := d.Del("b"); !ok || v.I != 1 || d.Len() != 19 || d.Keys()[1] != "c" {
		t.Error("Del")
	}
	d.Set("b", Int(9))
	if d.Keys()[19] != "b" {
		t.Error("re-set after Del appends")
	}
	if v := d.SetDefault("a", Int(5)); v.I != 0 {
		t.Error("SetDefault existing")
	}
	if v, ok := d.Get("t"); !ok || v.I != 19 {
		t.Error("Get after reindex")
	}
	c := d.Clone()
	c.Set("zz", Null)
	if d.Has("zz") || !c.Has("zz") {
		t.Error("Clone independence")
	}
}

func TestValueStrTruthHash(t *testing.T) {
	d := NewDict(0)
	d.Set("a", Int(1))
	d.Set("b", ListOf(S("x'y"), Null, B(true), F(1.5)))
	cases := map[string]Value{"None": Null, "True": B(true), "100.0": F(100), "nan": F(math.NaN()), "1e+16": F(1e16),
		"{'a': 1, 'b': [\"x'y\", None, True, 1.5]}": MapOf(d), "['a\\n']": ListOf(S("a\n"))}
	for want, v := range cases {
		if got := v.Str(); got != want {
			t.Errorf("Str = %q, want %q", got, want)
		}
	}
	for _, v := range []Value{Null, S(""), Int(0), F(0), B(false), ListOf(), MapOf(NewDict(0))} {
		if v.Truthy() {
			t.Errorf("%v truthy", v)
		}
	}
	if !F(math.NaN()).Truthy() || !S("0").Truthy() {
		t.Error("truthy")
	}
	if Int(1).HashKey() != F(1).HashKey() || B(true).HashKey() != Int(1).HashKey() || S("1").HashKey() == Int(1).HashKey() ||
		F(1.5).HashKey() == Int(1).HashKey() || F(1e20).HashKey() != Int(0).HashKey() && F(1e20).HashKey() != "n:100000000000000000000" {
		t.Error("HashKey")
	}
	if !Null.IsNoneOrEmpty() || !S("").IsNoneOrEmpty() || Int(0).IsNoneOrEmpty() || B(false).IsNoneOrEmpty() {
		t.Error("IsNoneOrEmpty")
	}
}

func TestDateTimeISO(t *testing.T) {
	cases := map[string]DateTime{
		"2026-10-14T09:30:00+00:00":        NewDateTime(2026, 10, 14, 9, 30, 0, 0, 0),
		"2026-10-14T09:30:00.032000+03:00": NewDateTime(2026, 10, 14, 9, 30, 0, 32000, 3*3600),
		"2026-01-02T03:04:05.000001-05:30": NewDateTime(2026, 1, 2, 3, 4, 5, 1, -(5*3600 + 1800)),
		"0100-01-01T00:00:00+05:30:15":     NewDateTime(100, 1, 1, 0, 0, 0, 0, 5*3600+30*60+15),
		"2026-10-14T09:30:00":              NewNaive(2026, 10, 14, 9, 30, 0, 0),
		"1970-01-01T00:00:00+00:00":        Epoch,
	}
	for want, d := range cases {
		if got := d.ISO(); got != want {
			t.Errorf("ISO = %q, want %q", got, want)
		}
	}
	a := NewDateTime(2026, 1, 1, 12, 0, 0, 0, 3*3600)
	b := NewDateTime(2026, 1, 1, 9, 0, 0, 0, 0)
	if a.Before(b) || b.Before(a) || !a.Equal(b) {
		t.Error("instant compare across offsets")
	}
}

// TestFoldClassFastPath: the ASCII fast path of foldClass equals the table lookup for every code point.
func TestFoldClassFastPath(t *testing.T) {
	slow := func(r rune) rune {
		l := unicode.ToLower(r)
		for _, fx := range pyFixes {
			for _, x := range fx {
				if x == l {
					return fx[0]
				}
			}
		}
		return l
	}
	for r := rune(0); r <= unicode.MaxRune; r++ {
		if got, want := foldClass(r), slow(r); got != want {
			t.Fatalf("foldClass(%U) = %U, want %U", r, got, want)
		}
	}
}
