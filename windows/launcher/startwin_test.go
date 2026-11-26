// Copyright 2026 LanEx Contributors
// Licensed under the Apache License, Version 2.0.

package main

import (
	"bytes"
	"strings"
	"testing"
	"time"
)

func TestStartProgressIsWeightedMonotonicAndNeverFullEarly(t *testing.T) {
	t0 := time.Date(2026, 11, 26, 10, 0, 0, 0, time.UTC)
	m := newStartModel(t0, [4]time.Duration{})
	last := 0.0
	for s := 0; s <= 300; s += 5 {
		p := m.progress(t0.Add(time.Duration(s) * time.Second))
		if p < last {
			t.Fatalf("progress went back at %ds: %v < %v", s, p, last)
		}
		if p >= startSteps[0].weight {
			t.Fatalf("step 1 alone reached its full share at %ds: %v", s, p)
		}
		last = p
	}
	m.advance(2, t0.Add(310*time.Second))
	if p := m.progress(t0.Add(310 * time.Second)); p < 0.6 || p >= 1 {
		t.Fatalf("after two steps: %v", p)
	}
	m.advance(3, t0.Add(311*time.Second))
	if p := m.progress(t0.Add(400 * time.Second)); p >= 1 {
		t.Fatalf("full before the window opened: %v", p)
	}
	m.advance(4, t0.Add(401*time.Second))
	if p := m.progress(t0.Add(401 * time.Second)); p != 1 {
		t.Fatalf("done should be full: %v", p)
	}
}

func TestStartSlowAfter45sOnlyOnEarlySteps(t *testing.T) {
	t0 := time.Now()
	m := newStartModel(t0, [4]time.Duration{})
	if m.slow(t0.Add(44 * time.Second)) {
		t.Fatal("slow too early")
	}
	if !m.slow(t0.Add(46 * time.Second)) {
		t.Fatal("not slow after 46 s on the WSL step")
	}
	m.advance(2, t0.Add(50*time.Second))
	if m.slow(t0.Add(200 * time.Second)) {
		t.Fatal("the server step is never 'slow WSL'")
	}
}

func TestStartTimesMedian(t *testing.T) {
	got := medianTimes([][4]float64{{10, 2, 5, 1}, {30, 4, 9, 1}, {20, 3, 7, 2}})
	want := [4]time.Duration{20 * time.Second, 3 * time.Second, 7 * time.Second, 1 * time.Second}
	if got != want {
		t.Fatalf("median: got %v want %v", got, want)
	}
}

func TestFailureButtonsPutTheSafeActionFirst(t *testing.T) {
	if b := buttonsFor(failMissing); b[0] != actRepair {
		t.Fatalf("missing distro: %v", b)
	}
	if b := buttonsFor(failTimeout); b[0] != actTryAgain || b[len(b)-1] != actQuit {
		t.Fatalf("timeout: %v", b)
	}
	for _, k := range []failKind{failTimeout, failExited, failStart, failWSL} {
		found := false
		for _, a := range buttonsFor(k) {
			found = found || a == actOpenLog
		}
		if !found {
			t.Fatalf("failure %d offers no way to the log", k)
		}
	}
}

func TestMarkerWriterMovesTheStartWindowAndKeepsTheLog(t *testing.T) {
	var log bytes.Buffer
	t0 := time.Now()
	sw := &startWin{model: newStartModel(t0, [4]time.Duration{}), created: make(chan struct{})}
	mw := &markerWriter{out: &log, sw: sw}
	mw.Write([]byte("LANEX-ST"))
	mw.Write([]byte("ART linux\nsome output\nLanEx is run"))
	if cur, _, _, _, _, _ := sw.model.snapshot(); cur != 1 {
		t.Fatalf("after the linux marker: step %d", cur)
	}
	mw.Write([]byte("ning at: http://127.0.0.1:8765/\n"))
	if cur, _, _, _, _, _ := sw.model.snapshot(); cur != 2 {
		t.Fatalf("after the server marker: step %d", cur)
	}
	if !strings.Contains(log.String(), "LanEx is running at: http://127.0.0.1:8765/") {
		t.Fatalf("log lost output: %q", log.String())
	}
}

func TestShareTextHidesUserAndProxy(t *testing.T) {
	s := shareText(`C:\Users\José Test\AppData x /mnt/c/Users/jose/y http://proxy.corp:8080/ https://ghcr.io/v2/`)
	if strings.Contains(s, "José") || strings.Contains(s, "jose") || strings.Contains(s, "proxy.corp") {
		t.Fatalf("not redacted: %q", s)
	}
	if !strings.Contains(s, "https://ghcr.io/v2/") {
		t.Fatalf("download site removed: %q", s)
	}
}
