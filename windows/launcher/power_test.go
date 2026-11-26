// Copyright 2026 LanEx Contributors
// Licensed under the Apache License, Version 2.0.

package main

import (
	"strings"
	"testing"
	"time"
)

func TestHoldAcquireAndRelease(t *testing.T) {
	var s holdState
	s.ac = -1
	t0 := time.Date(2026, 11, 26, 10, 0, 0, 0, time.UTC)
	if a := s.step(t0, holdAnswer{ok: true, hold: false}, 1); a != holdNone {
		t.Fatalf("idle: got %v", a)
	}
	if a := s.step(t0.Add(5*time.Second), holdAnswer{ok: true, hold: true}, 1); a != holdAcquire {
		t.Fatalf("run starts: got %v", a)
	}
	if a := s.step(t0.Add(10*time.Second), holdAnswer{ok: true, hold: true}, 1); a != holdNone {
		t.Fatalf("still running: got %v", a)
	}
	if a := s.step(t0.Add(15*time.Second), holdAnswer{ok: true, hold: false}, 1); a != holdRelease {
		t.Fatalf("run ends: got %v", a)
	}
}

func TestHoldGoneServerReleasesAfter30s(t *testing.T) {
	var s holdState
	s.ac = -1
	t0 := time.Date(2026, 11, 26, 10, 0, 0, 0, time.UTC)
	s.step(t0, holdAnswer{ok: true, hold: true}, -1)
	for i := 1; i <= 5; i++ { // 25 s of no answer: a restart, keep holding
		if a := s.step(t0.Add(time.Duration(i*5)*time.Second), holdAnswer{}, -1); a != holdNone {
			t.Fatalf("no answer at %ds: got %v", i*5, a)
		}
	}
	if a := s.step(t0.Add(30*time.Second), holdAnswer{}, -1); a != holdRelease {
		t.Fatalf("30 s without an answer: got %v", a)
	}
}

func TestHold404IsNoHold(t *testing.T) {
	var s holdState
	s.ac = -1
	t0 := time.Now()
	if a := s.step(t0, holdAnswer{gone: true}, -1); a != holdNone {
		t.Fatalf("404 while idle: got %v", a)
	}
	s.step(t0.Add(time.Second), holdAnswer{ok: true, hold: true}, -1)
	if a := s.step(t0.Add(2*time.Second), holdAnswer{gone: true}, -1); a != holdRelease {
		t.Fatalf("404 while held: got %v", a)
	}
}

func TestHoldRearmsAfterSleepAndPowerSourceChange(t *testing.T) {
	var s holdState
	s.ac = -1
	t0 := time.Date(2026, 11, 26, 10, 0, 0, 0, time.UTC)
	s.step(t0, holdAnswer{ok: true, hold: true}, 1)
	if a := s.step(t0.Add(2*time.Minute), holdAnswer{ok: true, hold: true}, 1); a != holdRearm {
		t.Fatalf("after a sleep gap: got %v", a)
	}
	if a := s.step(t0.Add(2*time.Minute+5*time.Second), holdAnswer{ok: true, hold: true}, 0); a != holdRearm {
		t.Fatalf("unplugged: got %v", a)
	}
	if a := s.step(t0.Add(2*time.Minute+10*time.Second), holdAnswer{ok: true, hold: true}, 0); a != holdNone {
		t.Fatalf("steady on battery: got %v", a)
	}
}

func TestParseHoldEnvelopeAndFlat(t *testing.T) {
	cases := map[string]holdAnswer{
		`{"ok":true,"data":{"hold":true,"reason":"run"}}`: {ok: true, hold: true},
		`{"ok":true,"data":{"hold":false}}`:               {ok: true, hold: false},
		`{"hold":true}`:                                   {ok: true, hold: true},
		`{"ok":false,"error":"x"}`:                        {},
		`not json`:                                        {},
	}
	for body, want := range cases {
		if got := parseHold(strings.NewReader(body)); got != want {
			t.Errorf("%s: got %+v want %+v", body, got, want)
		}
	}
}

func TestReasonContextLayout(t *testing.T) {
	// REASON_CONTEXT is 32 bytes on amd64; a wrong size makes PowerCreateRequest fail.
	if sz := sizeofReasonContext(); sz != 32 {
		t.Fatalf("REASON_CONTEXT size %d, want 32", sz)
	}
}
