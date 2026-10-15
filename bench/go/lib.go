package main

// The shared shape of a YAML library here: typed and generic loading, dumping, a document
// API where the library has one, and a patch API where it has one. A library fills in the
// functions it really has; a task it has no function for is `unsupported` with the reason.

import (
	"errors"
	"io"
	"reflect"
	"runtime/debug"
	"sync"
)

type docAPI struct {
	// parse reads the input into the library's lossless document (timed).
	parse func(in []byte, docs int) (any, error)
	// value reads the document back as Go values, one per document (for the guard only).
	value func(doc any) ([]any, error)
	// emit writes the document as text (timed in unchanged-emit and edit-script).
	emit func(doc any) ([]byte, error)
	// prepareEdit resolves the steps once, into the library's own addressing, before the clock.
	prepareEdit func(steps []EditStep) (func(doc any) error, error)
	// whyNoEdit says why the library has no edit through this API, when it has none.
	whyNoEdit string
}

type patchAPI struct {
	// prepare builds the library's patch from the operations, outside the clock.
	prepare func(ops []Op) (any, error)
	// apply applies it to the text and returns the new text (timed).
	apply func(in []byte, patch any) ([]byte, error)
	// valueOnly says the library cannot keep the text it patches.
	valueOnly bool
}

type yamlLib struct {
	id, pkg  string
	settings map[string]string
	// unmarshal reads one document into out (a pointer to a struct or an `any`).
	unmarshal func(in []byte, out any) error
	// decoder reads a stream document by document; nil when the library reads one document.
	decoder func(in []byte) func(out any) error
	marshal func(v any) ([]byte, error)

	doc          *docAPI
	whyNoDoc     string
	patch        *patchAPI
	whyNoPatch   string
	whyNoTyped   string
	whyNoGeneric string
	whyNoDump    string
}

func (l *yamlLib) ID() string                  { return l.id }
func (l *yamlLib) Package() string             { return l.pkg }
func (l *yamlLib) Role() string                { return "competitor" }
func (l *yamlLib) Settings() map[string]string { return l.settings }

// localRevisions names the commit of a library that is built from a local checkout, by module
// path, because a module replaced by a directory has no version of its own.
var localRevisions = map[string]string{}

// moduleVersion is the version of a module in this build, which is the version the lock file
// pinned.
func moduleVersion(path string) string {
	bi, ok := debug.ReadBuildInfo()
	if !ok {
		return "unknown"
	}
	for _, d := range bi.Deps {
		if d.Path == path {
			if d.Replace != nil {
				if d.Replace.Version == "" || d.Replace.Version == "(devel)" {
					// Replaced by a directory: the build says which commit it was.
					if rev, ok := localRevisions[path]; ok {
						return rev
					}
				}
				return d.Replace.Version
			}
			return d.Version
		}
	}
	return "unknown"
}

// loadAll reads every document of the input into Go values.
func (l *yamlLib) loadAll(in []byte, docs int) ([]any, error) {
	if docs <= 1 || l.decoder == nil {
		var v any
		if err := l.unmarshal(in, &v); err != nil {
			return nil, err
		}
		return []any{v}, nil
	}
	next := l.decoder(in)
	var out []any
	for {
		var v any
		err := next(&v)
		if errors.Is(err, io.EOF) {
			return out, nil
		}
		if err != nil {
			return nil, err
		}
		out = append(out, v)
	}
}

func treeProduct(docs []any) Product {
	conv := make([]any, len(docs))
	for i, d := range docs {
		c, err := fromAny(d)
		if err != nil {
			return Product{Kind: ValueProduct, Value: "cannot compare: " + err.Error()}
		}
		conv[i] = c
	}
	return Product{Kind: ValueProduct, Value: docsValue(conv), HasValue: true}
}

func bytesProduct(b []byte) Product { return Product{Kind: BytesProduct, Bytes: b} }

func typedOf[T any](parse func([]byte, any) error, in []byte) Runner {
	return newRunner(
		func() struct{} { return struct{}{} },
		func(*struct{}) (*T, error) {
			var t T
			if err := parse(in, &t); err != nil {
				return nil, err
			}
			return &t, nil
		},
		func(t *T) Product {
			v, err := fromTyped(reflect.ValueOf(t))
			if err != nil {
				return Product{Kind: ValueProduct, Value: "cannot compare: " + err.Error()}
			}
			return Product{Kind: ValueProduct, Value: v, HasValue: true}
		})
}

func unsupported(why, fallback string) (Runner, string) {
	if why == "" {
		why = fallback
	}
	return nil, why
}

func (l *yamlLib) Runner(task Task, w *Workload) (Runner, string) {
	in := w.Input
	switch task {
	case TypedLoad:
		if l.unmarshal == nil {
			return unsupported(l.whyNoTyped, "no typed decoding")
		}
		return typedRunner(l.unmarshal, w.Typed, in)
	case GenericLoad:
		if l.unmarshal == nil {
			return unsupported(l.whyNoGeneric, "no generic decoding")
		}
		return newRunner(
			func() struct{} { return struct{}{} },
			func(*struct{}) ([]any, error) { return l.loadAll(in, w.Documents) },
			treeProduct), ""
	case Dump:
		if l.marshal == nil {
			return unsupported(l.whyNoDump, "no encoder")
		}
		if w.Documents > 1 {
			return nil, "a stream of several documents is not dumped by this task"
		}
		return newRunner(
			func() any {
				// The library's own tree, read from the input, outside the clock.
				docs, err := l.loadAll(in, 1)
				if err != nil {
					return err
				}
				return docs[0]
			},
			func(tree *any) ([]byte, error) {
				if err, bad := (*tree).(error); bad {
					return nil, err
				}
				return l.marshal(*tree)
			},
			bytesProduct), ""
	case DocumentParse, UnchangedEmit, EditScript:
		return l.docRunner(task, w)
	case PatchTask:
		if l.patch == nil {
			return unsupported(l.whyNoPatch, "no patch support")
		}
		return l.patchRunner(w)
	}
	return nil, "unknown task"
}

func (l *yamlLib) docRunner(task Task, w *Workload) (Runner, string) {
	d := l.doc
	if d == nil {
		return unsupported(l.whyNoDoc, "no lossless document")
	}
	in := w.Input
	parseState := func() any {
		doc, err := d.parse(in, w.Documents)
		if err != nil {
			return err
		}
		return doc
	}
	switch task {
	case DocumentParse:
		return newRunner(
			func() struct{} { return struct{}{} },
			func(*struct{}) (any, error) { return d.parse(in, w.Documents) },
			func(doc any) Product {
				p := Product{Kind: DocumentProduct}
				if d.value != nil {
					docs, err := d.value(doc)
					if err != nil {
						p.Value = "cannot read the document back: " + err.Error()
						p.HasValue = true
					} else {
						p = treeDoc(docs)
					}
				}
				if b, err := d.emit(doc); err == nil {
					p.Bytes = b
				}
				return p
			}), ""
	case UnchangedEmit:
		// Writing out an unchanged document does not touch it, and it can take a few
		// nanoseconds, which makes the benchmark ask for millions of runs. Parsing a fresh
		// document for each of them (outside the clock) would take far longer than the
		// measurement, so every run starts from one parsed document.
		var once sync.Once
		var shared any
		return newRunner(func() any {
			once.Do(func() { shared = parseState() })
			return shared
		},
			func(doc *any) ([]byte, error) {
				if err, bad := (*doc).(error); bad {
					return nil, err
				}
				return d.emit(*doc)
			}, bytesProduct), ""
	default: // EditScript
		if d.prepareEdit == nil {
			return unsupported(d.whyNoEdit, "no edit API")
		}
		steps, err := resolveSteps(w, w.EditOps)
		if err != nil {
			return nil, "the workload's edit cannot be resolved: " + err.Error()
		}
		edit, err := d.prepareEdit(steps)
		if err != nil {
			return nil, err.Error()
		}
		return newRunner(parseState,
			func(doc *any) ([]byte, error) {
				if err, bad := (*doc).(error); bad {
					return nil, err
				}
				if err := edit(*doc); err != nil {
					return nil, err
				}
				return d.emit(*doc)
			}, bytesProduct), ""
	}
}

func treeDoc(docs []any) Product {
	p := treeProduct(docs)
	p.Kind = DocumentProduct
	return p
}

func (l *yamlLib) patchRunner(w *Workload) (Runner, string) {
	pa := l.patch
	in := w.Input
	good, err := pa.prepare(w.PatchOps)
	if err != nil {
		return nil, "the patch cannot be built: " + err.Error()
	}
	bad, err := pa.prepare(w.PatchFailing)
	if err != nil {
		return nil, "the failing patch cannot be built: " + err.Error()
	}
	return newPatchRunner(
		func() struct{} { return struct{}{} },
		func(*struct{}) ([]byte, error) { return pa.apply(in, good) },
		func(b []byte) Product {
			p := bytesProduct(b)
			p.ValueOnly = pa.valueOnly
			return p
		},
		func() FailedPatch {
			// The library takes the text and returns new text, so the document afterwards is
			// the text the caller still holds: its own copy, compared with the original.
			held := append([]byte(nil), in...)
			_, err := pa.apply(held, bad)
			return FailedPatch{Errored: err != nil, Bytes: held}
		}), ""
}
