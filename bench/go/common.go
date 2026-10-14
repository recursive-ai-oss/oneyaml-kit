package main

// The common form of a value: what the guard compares. It has exactly these Go types:
// nil, bool, string, Int, float64, []any and map[string]any. An integer and a float with the
// same digits are different values, and so are "1" and 1.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"time"
)

// Int is an integer kept as its decimal digits, so no width limits what can be compared.
type Int string

// fromJSON reads JSON text into the common form, telling integers from floats by their text.
func fromJSON(b []byte) (any, error) {
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.UseNumber()
	var v any
	if err := dec.Decode(&v); err != nil {
		return nil, err
	}
	return normJSON(v), nil
}

func normJSON(v any) any {
	switch x := v.(type) {
	case json.Number:
		s := string(x)
		if strings.ContainsAny(s, ".eE") {
			f, _ := strconv.ParseFloat(s, 64)
			return f
		}
		return Int(s)
	case []any:
		for i := range x {
			x[i] = normJSON(x[i])
		}
	case map[string]any:
		for k := range x {
			x[k] = normJSON(x[k])
		}
	}
	return v
}

// keyString is the text of a mapping key that is not a string. JSON, the form the
// expectations are written in, has only string keys.
func keyString(k any) (string, error) {
	switch x := k.(type) {
	case string:
		return x, nil
	case nil:
		return "null", nil
	case bool:
		return strconv.FormatBool(x), nil
	case float64:
		return strconv.FormatFloat(x, 'g', -1, 64), nil
	case Int:
		// The reference reading builds integers as Int already.
		return string(x), nil
	}
	v, err := fromAny(k)
	if err != nil {
		return "", err
	}
	if i, ok := v.(Int); ok {
		return string(i), nil
	}
	return "", fmt.Errorf("mapping key of Go type %T", k)
}

// fromAny converts what a library decoded into an `any` to the common form. A Go type the
// converter does not know is an error, never a guess; a timestamp the library resolved is
// written as a marked text, so it differs from the plain text the specification gives.
func fromAny(v any) (any, error) {
	switch x := v.(type) {
	case nil:
		return nil, nil
	case bool:
		return x, nil
	case string:
		return x, nil
	case int:
		return Int(strconv.FormatInt(int64(x), 10)), nil
	case int8:
		return Int(strconv.FormatInt(int64(x), 10)), nil
	case int16:
		return Int(strconv.FormatInt(int64(x), 10)), nil
	case int32:
		return Int(strconv.FormatInt(int64(x), 10)), nil
	case int64:
		return Int(strconv.FormatInt(x, 10)), nil
	case uint:
		return Int(strconv.FormatUint(uint64(x), 10)), nil
	case uint8:
		return Int(strconv.FormatUint(uint64(x), 10)), nil
	case uint16:
		return Int(strconv.FormatUint(uint64(x), 10)), nil
	case uint32:
		return Int(strconv.FormatUint(uint64(x), 10)), nil
	case uint64:
		return Int(strconv.FormatUint(x, 10)), nil
	case float32:
		return float64(x), nil
	case float64:
		return x, nil
	case json.Number:
		return normJSON(x), nil
	case time.Time:
		return "timestamp " + x.Format(time.RFC3339Nano), nil
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			c, err := fromAny(e)
			if err != nil {
				return nil, err
			}
			out[i] = c
		}
		return out, nil
	case map[string]any:
		out := make(map[string]any, len(x))
		for k, e := range x {
			c, err := fromAny(e)
			if err != nil {
				return nil, err
			}
			out[k] = c
		}
		return out, nil
	case map[any]any:
		out := make(map[string]any, len(x))
		for k, e := range x {
			ks, err := keyString(k)
			if err != nil {
				return nil, err
			}
			c, err := fromAny(e)
			if err != nil {
				return nil, err
			}
			out[ks] = c
		}
		return out, nil
	}
	return nil, fmt.Errorf("value of Go type %T", v)
}

// fromTyped converts a decoded struct (or slice, or map) to the common form by its `json`
// tags. A nil pointer is null, unless the field is marked `kit:"skip-none"`, which leaves the
// key out the way an omitempty field would.
func fromTyped(rv reflect.Value) (any, error) {
	switch rv.Kind() {
	case reflect.Interface:
		if rv.IsNil() {
			return nil, nil
		}
		return fromAny(rv.Interface())
	case reflect.Pointer:
		if rv.IsNil() {
			return nil, nil
		}
		return fromTyped(rv.Elem())
	case reflect.Struct:
		out := map[string]any{}
		t := rv.Type()
		for i := 0; i < t.NumField(); i++ {
			f := t.Field(i)
			name, _, _ := strings.Cut(f.Tag.Get("json"), ",")
			if name == "-" || !f.IsExported() {
				continue
			}
			if name == "" {
				name = f.Name
			}
			fv := rv.Field(i)
			if (fv.Kind() == reflect.Pointer || fv.Kind() == reflect.Interface || fv.Kind() == reflect.Map) && fv.IsNil() &&
				f.Tag.Get("kit") == "skip-none" {
				continue
			}
			c, err := fromTyped(fv)
			if err != nil {
				return nil, err
			}
			out[name] = c
		}
		return out, nil
	case reflect.Slice, reflect.Array:
		out := make([]any, rv.Len())
		for i := range out {
			c, err := fromTyped(rv.Index(i))
			if err != nil {
				return nil, err
			}
			out[i] = c
		}
		return out, nil
	case reflect.Map:
		out := make(map[string]any, rv.Len())
		it := rv.MapRange()
		for it.Next() {
			k := it.Key()
			if k.Kind() != reflect.String {
				return nil, fmt.Errorf("map key of Go type %s", k.Type())
			}
			c, err := fromTyped(it.Value())
			if err != nil {
				return nil, err
			}
			out[k.String()] = c
		}
		return out, nil
	case reflect.String:
		return rv.String(), nil
	case reflect.Bool:
		return rv.Bool(), nil
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		return Int(strconv.FormatInt(rv.Int(), 10)), nil
	case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64:
		return Int(strconv.FormatUint(rv.Uint(), 10)), nil
	case reflect.Float32, reflect.Float64:
		return rv.Float(), nil
	}
	return nil, fmt.Errorf("field of Go kind %s", rv.Kind())
}

// docsValue is the common form of a list of documents: the value itself for one document,
// an array for several.
func docsValue(docs []any) any {
	if len(docs) == 1 {
		return docs[0]
	}
	if docs == nil {
		docs = []any{}
	}
	return docs
}

func clip(s string) string {
	r := []rune(s)
	if len(r) > 80 {
		return string(r[:80]) + "…"
	}
	return s
}

func show(v any) string {
	switch x := v.(type) {
	case Int:
		return string(x)
	case float64:
		return strconv.FormatFloat(x, 'g', -1, 64) + " (float)"
	case string:
		return strconv.Quote(x)
	case nil:
		return "null"
	case []any:
		return fmt.Sprintf("a sequence of %d", len(x))
	case map[string]any:
		return fmt.Sprintf("a mapping of %d", len(x))
	}
	return fmt.Sprint(v)
}

func pointerEscape(k string) string {
	return strings.ReplaceAll(strings.ReplaceAll(k, "~", "~0"), "/", "~1")
}

// firstDiff says where two values first differ, as a pointer and the two sides; "" if equal.
func firstDiff(got, want any, at string) string {
	switch w := want.(type) {
	case map[string]any:
		g, ok := got.(map[string]any)
		if !ok {
			return diffAt(at, got, want)
		}
		keys := make([]string, 0, len(w))
		for k := range w {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			p := at + "/" + pointerEscape(k)
			gv, present := g[k]
			if !present {
				return "missing " + p
			}
			if d := firstDiff(gv, w[k], p); d != "" {
				return d
			}
		}
		var extra []string
		for k := range g {
			if _, ok := w[k]; !ok {
				extra = append(extra, k)
			}
		}
		if len(extra) > 0 {
			sort.Strings(extra)
			return "unexpected " + at + "/" + pointerEscape(extra[0])
		}
		return ""
	case []any:
		g, ok := got.([]any)
		if !ok {
			return diffAt(at, got, want)
		}
		for i := 0; i < len(g) && i < len(w); i++ {
			if d := firstDiff(g[i], w[i], fmt.Sprintf("%s/%d", at, i)); d != "" {
				return d
			}
		}
		if len(g) != len(w) {
			return fmt.Sprintf("%s: %d items, expected %d", at, len(g), len(w))
		}
		return ""
	case float64:
		if g, ok := got.(float64); ok && (g == w || (math.IsNaN(g) && math.IsNaN(w))) {
			return ""
		}
	default:
		if got == want {
			return ""
		}
	}
	return diffAt(at, got, want)
}

func diffAt(at string, got, want any) string {
	if at == "" {
		at = "/"
	}
	// A long pointer keeps its end, where the difference is, and the two values are never cut off
	// by the length of the path.
	if r := []rune(at); len(r) > 48 {
		at = "…" + string(r[len(r)-47:])
	}
	return fmt.Sprintf("at %s: got %s, expected %s", at, clip(show(got)), clip(show(want)))
}

// bytesDiff says where two byte strings first differ; "" if they are equal.
func bytesDiff(got, want []byte) string {
	if bytes.Equal(got, want) {
		return ""
	}
	at := 0
	for at < len(got) && at < len(want) && got[at] == want[at] {
		at++
	}
	ctx := func(b []byte) string {
		lo := max(0, at-12)
		hi := min(len(b), at+24)
		if lo > hi {
			lo = hi
		}
		return clip(fmt.Sprintf("%q", string(b[lo:hi])))
	}
	return fmt.Sprintf("byte %d: got %d bytes %s, expected %d bytes %s; %s", at, len(got), ctx(got), len(want), ctx(want),
		explainBytes(got, want))
}

// A normalisation hides one kind of difference between two texts. The smallest set of kinds that
// makes the texts equal says what the difference is made of.
type normaliser struct {
	name string
	fn   func(string) string
}

var normalisers = []normaliser{
	{"blank lines", func(s string) string {
		var out []string
		for _, l := range strings.Split(s, "\n") {
			if strings.TrimSpace(l) != "" {
				out = append(out, l)
			}
		}
		return strings.Join(out, "\n")
	}},
	{"trailing spaces", func(s string) string {
		ls := strings.Split(s, "\n")
		for i, l := range ls {
			ls[i] = strings.TrimRight(l, " \t")
		}
		return strings.Join(ls, "\n")
	}},
	{"indentation", func(s string) string {
		ls := strings.Split(s, "\n")
		for i, l := range ls {
			ls[i] = strings.TrimLeft(l, " \t")
		}
		return strings.Join(ls, "\n")
	}},
	{"spacing inside lines", func(s string) string {
		ls := strings.Split(s, "\n")
		for i, l := range ls {
			t := strings.TrimLeft(l, " \t")
			ls[i] = l[:len(l)-len(t)] + strings.Join(strings.FieldsFunc(t, func(r rune) bool { return r == ' ' || r == '\t' }), " ")
		}
		return strings.Join(ls, "\n")
	}},
	{"quoting", func(s string) string { return strings.NewReplacer("'", "", "\"", "").Replace(s) }},
	{"line breaks", func(s string) string { return strings.ReplaceAll(strings.ReplaceAll(s, "\r\n", "\n"), "\r", "\n") }},
}

// explainBytes says what two different texts differ in: the smallest set of the kinds above that
// makes them equal, or else the first line that differs.
func explainBytes(got, want []byte) string {
	g, w := string(got), string(want)
	n := len(normalisers)
	best := -1
	for mask := 1; mask < 1<<n; mask++ {
		a, b := g, w
		for i, nm := range normalisers {
			if mask&(1<<i) != 0 {
				a, b = nm.fn(a), nm.fn(b)
			}
		}
		if a == b && (best < 0 || bits(mask) < bits(best)) {
			best = mask
		}
	}
	if best >= 0 {
		var names []string
		for i, nm := range normalisers {
			if best&(1<<i) != 0 {
				names = append(names, nm.name)
			}
		}
		return "the texts differ only in " + strings.Join(names, " and ")
	}
	gl, wl := strings.Split(g, "\n"), strings.Split(w, "\n")
	for i := 0; i < len(gl) && i < len(wl); i++ {
		if gl[i] != wl[i] {
			return fmt.Sprintf("line %d: got %s, expected %s", i+1, quoteClip(gl[i]), quoteClip(wl[i]))
		}
	}
	return fmt.Sprintf("got %d lines, expected %d", len(gl), len(wl))
}

// quoteClip quotes a line, cutting a long one inside the quotes so the quote is always closed.
func quoteClip(s string) string {
	if r := []rune(s); len(r) > 60 {
		s = string(r[:60]) + "…"
	}
	return strconv.Quote(s)
}

func bits(m int) int {
	n := 0
	for ; m > 0; m &= m - 1 {
		n++
	}
	return n
}
