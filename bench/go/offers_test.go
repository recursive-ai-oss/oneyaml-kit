package main

import "testing"

type partial struct{ yamlLib }

func (partial) Tasks() []Task { return []Task{GenericLoad} }

func TestOffers(t *testing.T) {
	if !offers(&yamlLib{}, TypedLoad) {
		t.Error("a library without a task list takes part in every task")
	}
	p := &partial{}
	if !offers(p, GenericLoad) || offers(p, TypedLoad) {
		t.Error("a library with a task list takes part in those tasks only")
	}
}
