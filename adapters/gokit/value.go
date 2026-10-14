package gokit

import (
	"bytes"
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
