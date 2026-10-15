package gokit

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"math/big"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

// Documents writes a list of Go values, one per YAML document, as a kit value.
// The writer reports what the library returned and nothing more: a value of a kind it
// does not know is an error, never a guess.
func Documents(docs []any) ([]byte, error) {
	var b bytes.Buffer
	b.WriteByte('[')
	for i, d := range docs {
		if i > 0 {
			b.WriteByte(',')
		}
		if err := writeValue(&b, d, 0); err != nil {
			return nil, err
		}
	}
	b.WriteByte(']')
	return b.Bytes(), nil
}

func js(s string) []byte {
	var b bytes.Buffer
	enc := json.NewEncoder(&b)
	enc.SetEscapeHTML(false)
	_ = enc.Encode(s)
	return bytes.TrimRight(b.Bytes(), "\n")
}

// Float formats a finite float so that it always reads back as a float.
func Float(f float64) string {
	s := strconv.FormatFloat(f, 'g', -1, 64)
	if !strings.ContainsAny(s, ".eE") {
		s += ".0"
	}
	return s
}

func writeFloat(b *bytes.Buffer, f float64) {
	switch {
	case math.IsNaN(f):
		b.WriteString(`{"$float":"nan"}`)
	case math.IsInf(f, 1):
		b.WriteString(`{"$float":"inf"}`)
	case math.IsInf(f, -1):
		b.WriteString(`{"$float":"-inf"}`)
	default:
		b.WriteString(Float(f))
	}
}

func writeValue(b *bytes.Buffer, v any, depth int) error {
	if depth > 1_000_000 {
		return errors.New("value nested too deeply for the adapter")
	}
	switch x := v.(type) {
	case nil:
		b.WriteString("null")
	case bool:
		b.WriteString(strconv.FormatBool(x))
	case string:
		b.Write(js(x))
	case []byte:
		// The kit has no binary type. A library that decodes a binary scalar into bytes has
		// not kept the text, and the bytes are written as the string they spell.
		b.Write(js(string(x)))
	case int:
		b.WriteString(strconv.FormatInt(int64(x), 10))
	case int8:
		b.WriteString(strconv.FormatInt(int64(x), 10))
	case int16:
		b.WriteString(strconv.FormatInt(int64(x), 10))
	case int32:
		b.WriteString(strconv.FormatInt(int64(x), 10))
	case int64:
		b.WriteString(strconv.FormatInt(x, 10))
	case uint:
		b.WriteString(strconv.FormatUint(uint64(x), 10))
	case uint8:
		b.WriteString(strconv.FormatUint(uint64(x), 10))
	case uint16:
		b.WriteString(strconv.FormatUint(uint64(x), 10))
	case uint32:
		b.WriteString(strconv.FormatUint(uint64(x), 10))
	case uint64:
		b.WriteString(strconv.FormatUint(x, 10))
	case *big.Int:
		b.WriteString(x.String())
	case float32:
		writeFloat(b, float64(x))
	case float64:
		writeFloat(b, x)
	case json.Number:
		return writeNumber(b, string(x))
	case time.Time:
		// The kit's core schema has no timestamp type. A library that returns one has
		// resolved a plain scalar to something the schema leaves as text.
		b.Write(js(x.Format(time.RFC3339Nano)))
	case []any:
		b.WriteByte('[')
		for i, e := range x {
			if i > 0 {
				b.WriteByte(',')
			}
			if err := writeValue(b, e, depth+1); err != nil {
				return err
			}
		}
		b.WriteByte(']')
	case map[string]any:
		keys := make([]string, 0, len(x))
		plain := true
		for k := range x {
			keys = append(keys, k)
			if strings.HasPrefix(k, "$") {
				plain = false
			}
		}
		sort.Strings(keys)
		if plain {
			b.WriteByte('{')
			for i, k := range keys {
				if i > 0 {
					b.WriteByte(',')
				}
				b.Write(js(k))
				b.WriteByte(':')
				if err := writeValue(b, x[k], depth+1); err != nil {
					return err
				}
			}
			b.WriteByte('}')
			return nil
		}
		pairs := make([][2]any, 0, len(keys))
		for _, k := range keys {
			pairs = append(pairs, [2]any{k, x[k]})
		}
		return writePairs(b, pairs, depth)
	case map[any]any:
		pairs := make([][2]any, 0, len(x))
		plain := true
		for k, e := range x {
			pairs = append(pairs, [2]any{k, e})
			if s, ok := k.(string); !ok || strings.HasPrefix(s, "$") {
				plain = false
			}
		}
		sort.Slice(pairs, func(i, j int) bool {
			return fmt.Sprintf("%T%v", pairs[i][0], pairs[i][0]) < fmt.Sprintf("%T%v", pairs[j][0], pairs[j][0])
		})
		if plain {
			b.WriteByte('{')
			for i, p := range pairs {
				if i > 0 {
					b.WriteByte(',')
				}
				b.Write(js(p[0].(string)))
				b.WriteByte(':')
				if err := writeValue(b, p[1], depth+1); err != nil {
					return err
				}
			}
			b.WriteByte('}')
			return nil
		}
		return writePairs(b, pairs, depth)
	default:
		return fmt.Errorf("value of Go type %s", reflect.TypeOf(v))
	}
	return nil
}

func writePairs(b *bytes.Buffer, pairs [][2]any, depth int) error {
	b.WriteString(`{"$map":[`)
	for i, p := range pairs {
		if i > 0 {
			b.WriteByte(',')
		}
		b.WriteByte('[')
		if err := writeValue(b, p[0], depth+1); err != nil {
			return err
		}
		b.WriteByte(',')
		if err := writeValue(b, p[1], depth+1); err != nil {
			return err
		}
		b.WriteByte(']')
	}
	b.WriteString(`]}`)
	return nil
}

// writeNumber writes a JSON number by its text: no decimal point or exponent is an
// integer, anything else is a float, so 1 and 1.0 stay different.
func writeNumber(b *bytes.Buffer, s string) error {
	if s == "" {
		return errors.New("empty number")
	}
	if strings.ContainsAny(s, ".eE") {
		f, err := strconv.ParseFloat(s, 64)
		if err != nil {
			return err
		}
		writeFloat(b, f)
		return nil
	}
	b.WriteString(s)
	return nil
}

// FromJSON reads JSON text (as a library that converts YAML to JSON returns it) into
// Go values that Documents can write, keeping integers and floats apart.
func FromJSON(data []byte) (any, error) {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	var v any
	if err := dec.Decode(&v); err != nil {
		return nil, err
	}
	return v, nil
}

// Native converts a kit value (JSON, with the escape forms of docs/case-format.md) to Go data
// for a library to write: an integer is an int64 (a uint64 above that), a number with a point
// or an exponent is a float64, an object is a map[string]any and an array a []any. A mapping
// with keys that are not strings cannot be a map[string]any and is declined.
func Native(raw json.RawMessage) (any, error) { return NativeWith(raw, nil) }

// BinaryTag is the tag of a kit value that is a byte string: a `$tagged` node with this tag holds
// the base64 of the bytes.
const BinaryTag = "tag:yaml.org,2002:binary"

// NativeWith is Native for a library that has a way to write a byte string. The function turns
// the bytes into the Go value the library writes as a binary scalar. With nil, a byte string is
// declined: the adapter reports the case unsupported instead of writing something else.
func NativeWith(raw json.RawMessage, binary func([]byte) (any, error)) (any, error) {
	if len(raw) == 0 {
		return nil, errors.New("no value")
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var v any
	if err := dec.Decode(&v); err != nil {
		return nil, err
	}
	return nativeOf(v, 0, binary)
}

func nativeOf(v any, depth int, binary func([]byte) (any, error)) (any, error) {
	if depth > 1000 {
		return nil, errors.New("value nesting too deep for the adapter")
	}
	switch x := v.(type) {
	case json.Number:
		if !strings.ContainsAny(string(x), ".eE") {
			if i, err := strconv.ParseInt(string(x), 10, 64); err == nil {
				return i, nil
			}
			if u, err := strconv.ParseUint(string(x), 10, 64); err == nil {
				return u, nil
			}
			return nil, errors.New("integer beyond 64 bits")
		}
		return strconv.ParseFloat(string(x), 64)
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			c, err := nativeOf(e, depth+1, binary)
			if err != nil {
				return nil, err
			}
			out[i] = c
		}
		return out, nil
	case map[string]any:
		if f, ok := x["$float"].(string); ok && len(x) == 1 {
			switch f {
			case "inf":
				return math.Inf(1), nil
			case "-inf":
				return math.Inf(-1), nil
			}
			return math.NaN(), nil
		}
		if tagged, ok := x["$tagged"].(map[string]any); ok && len(x) == 1 {
			text, isText := tagged["value"].(string)
			if tagged["tag"] != BinaryTag || !isText {
				return nil, errors.New("tagged value")
			}
			if binary == nil {
				return nil, errors.New("the library has no byte string it writes as !!binary")
			}
			data, err := base64.StdEncoding.DecodeString(text)
			if err != nil {
				return nil, err
			}
			return binary(data)
		}
		if _, ok := x["$map"]; ok && len(x) == 1 {
			return nil, errors.New("mapping with keys that are not strings")
		}
		out := make(map[string]any, len(x))
		for k, e := range x {
			c, err := nativeOf(e, depth+1, binary)
			if err != nil {
				return nil, err
			}
			out[k] = c
		}
		return out, nil
	}
	return v, nil
}

// InvalidUTF8String hands bytes to a library as a Go string, for libraries that write a string
// that is not valid UTF-8 as a `!!binary` scalar. Bytes that are valid UTF-8 would be written as
// text, so they are declined: the library has no way to write them as binary.
func InvalidUTF8String(data []byte) (any, error) {
	if utf8.Valid(data) {
		return nil, errors.New("bytes that are valid UTF-8 are written as text, not as !!binary")
	}
	return string(data), nil
}
