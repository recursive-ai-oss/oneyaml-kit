// Package gokit is the part of a Go kit adapter that does not depend on the library
// under test: the JSON Lines protocol loop, the kit's value notation and the pieces of
// RFC 6901 and RFC 6902 that an adapter has to supply itself.
//
// An adapter never judges anything. It decodes a request, calls its library, and
// converts what the library returned into the shapes the protocol describes.
package gokit

import (
	"bufio"
	"bytes"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"runtime/debug"
	"strconv"
)

// Hello is the first line an adapter sends.
type Hello struct {
	Name         string
	Version      string
	Capabilities []string
	// Emulates names every behaviour the adapter supplies on the library's behalf.
	Emulates []string
	Info     map[string]string
}

// PatchArgs is the operation object of a patch case.
type PatchArgs struct {
	Patch    json.RawMessage `json:"patch"`
	Format   string          `json:"format"`
	Document int             `json:"document"`
	Profile  string          `json:"profile"`
}

// Adapter is what a library binding supplies. A nil function means the operation is
// not offered, and the kit does not send cases that need it.
type Adapter struct {
	Hello  Hello
	Events func(input []byte) Result
	Value  func(input []byte) Result
	Patch  func(input []byte, args PatchArgs) Result
}

// Result is one response. Build it with the constructors below.
type Result struct {
	status   string
	events   []string
	value    []byte
	output   []byte
	hasOut   bool
	reason   string
	errCat   string
	errMsg   string
	line     int
	column   int
	emulated []string
}

// OKEvents answers an events case.
func OKEvents(lines []string) Result { return Result{status: "ok", events: lines} }

// OKValue answers a value case with the documents as a kit value JSON array.
func OKValue(json []byte) Result { return Result{status: "ok", value: json} }

// OKOutput answers a patch case with the whole stream after the patch.
func OKOutput(b []byte) Result { return Result{status: "ok", output: b, hasOut: true} }

// Accepted says the library took the input and the adapter has no result to describe,
// which is all a library without an event stream can say.
func Accepted() Result { return Result{status: "accepted"} }

// Unsupported declines one case, with a short reason.
func Unsupported(reason string) Result { return Result{status: "unsupported", reason: reason} }

// Err reports an error. Category is one of the kit's names or "other". Line and column
// are 1-based, or zero when the library does not say.
func Err(category, message string, line, column int) Result {
	return Result{status: "error", errCat: category, errMsg: message, line: line, column: column}
}

// WithOutput adds the bytes the document has after a failed patch.
func (r Result) WithOutput(b []byte) Result { r.output, r.hasOut = b, true; return r }

// WithEmulated names the declared behaviours this answer depended on.
func (r Result) WithEmulated(names ...string) Result {
	r.emulated = append(append([]string{}, r.emulated...), names...)
	return r
}

type request struct {
	Type  string          `json:"type"`
	ID    string          `json:"id"`
	Op    string          `json:"op"`
	Input string          `json:"input"`
	Args  json.RawMessage `json:"args"`
}

func quote(s string) []byte {
	b, _ := json.Marshal(s)
	return b
}

func quoteList(l []string) []byte {
	if l == nil {
		l = []string{}
	}
	b, _ := json.Marshal(l)
	return b
}

func (r Result) encode(id string) []byte {
	var b bytes.Buffer
	b.WriteString(`{"id":`)
	b.Write(quote(id))
	b.WriteString(`,"status":`)
	b.Write(quote(r.status))
	switch r.status {
	case "unsupported":
		b.WriteString(`,"reason":`)
		b.Write(quote(r.reason))
	case "error":
		b.WriteString(`,"error":{"category":`)
		b.Write(quote(r.errCat))
		b.WriteString(`,"message":`)
		b.Write(quote(r.errMsg))
		if r.line > 0 {
			b.WriteString(`,"line":` + strconv.Itoa(r.line))
			if r.column > 0 {
				b.WriteString(`,"column":` + strconv.Itoa(r.column))
			}
		}
		b.WriteString(`}`)
	}
	if r.events != nil {
		b.WriteString(`,"events":`)
		b.Write(quoteList(r.events))
	}
	if r.value != nil {
		b.WriteString(`,"value":`)
		b.Write(r.value)
	}
	if r.hasOut {
		b.WriteString(`,"output":"` + base64.StdEncoding.EncodeToString(r.output) + `"`)
	}
	if len(r.emulated) > 0 {
		b.WriteString(`,"emulated":`)
		b.Write(quoteList(r.emulated))
	}
	b.WriteString("}\n")
	return b.Bytes()
}

func (a *Adapter) hello() []byte {
	m := map[string]any{
		"type": "hello", "protocol": 2, "name": a.Hello.Name, "version": a.Hello.Version,
		"language": "go", "capabilities": nonNil(a.Hello.Capabilities),
		"emulates": nonNil(a.Hello.Emulates), "info": a.Hello.Info,
	}
	b, _ := json.Marshal(m)
	return append(b, '\n')
}

func nonNil(l []string) []string {
	if l == nil {
		return []string{}
	}
	return l
}

func (a *Adapter) handle(q request) Result {
	input, err := base64.StdEncoding.DecodeString(q.Input)
	if err != nil {
		return Unsupported("input is not base64")
	}
	var args PatchArgs
	if len(q.Args) > 0 {
		_ = json.Unmarshal(q.Args, &args)
	}
	if args.Profile != "" && args.Profile != "core" {
		return Unsupported("profile " + args.Profile)
	}
	switch q.Op {
	case "events":
		if a.Events != nil {
			return a.Events(input)
		}
	case "value":
		if a.Value != nil {
			return a.Value(input)
		}
	case "patch":
		if a.Patch != nil {
			return a.Patch(input, args)
		}
	}
	return Unsupported("operation " + q.Op)
}

// Serve answers requests on standard input until "bye" or the end of input.
//
// A panic inside the library ends the process. Turning it into an error answer would
// let a crash pass as a correct rejection of invalid input; the kit restarts the
// adapter and counts the case as a failure.
func (a *Adapter) Serve() {
	// Deeply nested input is walked recursively by some libraries and by the value writer.
	debug.SetMaxStack(1 << 30)
	in := bufio.NewReaderSize(os.Stdin, 1<<20)
	out := bufio.NewWriterSize(os.Stdout, 1<<16)
	for {
		line, err := in.ReadBytes('\n')
		if len(bytes.TrimSpace(line)) > 0 {
			var q request
			if json.Unmarshal(line, &q) != nil {
				return
			}
			switch q.Type {
			case "bye":
				return
			case "hello":
				out.Write(a.hello())
			case "run":
				out.Write(a.run(q))
			}
			if out.Flush() != nil {
				return
			}
		}
		if err != nil {
			if err != io.EOF {
				fmt.Fprintln(os.Stderr, "read:", err)
			}
			return
		}
	}
}

func (a *Adapter) run(q request) (resp []byte) {
	defer func() {
		if v := recover(); v != nil {
			fmt.Fprintf(os.Stderr, "panic in %s: %v\n%s", q.ID, v, debug.Stack())
			os.Exit(70)
		}
	}()
	return a.handle(q).encode(q.ID)
}
