// Copyright 2026 LanEx Contributors
// Licensed under the Apache License, Version 2.0.

package main

import (
	"os"
	"strconv"
	"testing"
	"time"
)

// TestLiveHoldAgainstServer polls a running LanEx server the way the launcher
// does (fetchHold + holdState, the same 5 s step) and reports each change.
// It needs a server and a run started while it polls, so it is skipped unless
// LANEX_LIVE_HOLD_PORT and LANEX_LIVE_HOLD_KEY are set:
//
//	LANEX_LIVE_HOLD_PORT=8836 LANEX_LIVE_HOLD_KEY=... LANEX_LIVE_HOLD_SECS=300 \
//	  go test -run TestLiveHoldAgainstServer -v .
//
// It passes when it saw the server ask for a hold and, later, let it go.
// No power request is taken: this checks the conversation, not Windows.
func TestLiveHoldAgainstServer(t *testing.T) {
	port, _ := strconv.Atoi(os.Getenv("LANEX_LIVE_HOLD_PORT"))
	key := os.Getenv("LANEX_LIVE_HOLD_KEY")
	if port == 0 || key == "" {
		t.Skip("set LANEX_LIVE_HOLD_PORT and LANEX_LIVE_HOLD_KEY to run against a live server")
	}
	secs, _ := strconv.Atoi(os.Getenv("LANEX_LIVE_HOLD_SECS"))
	if secs <= 0 {
		secs = 300
	}
	var s holdState
	s.ac = -1
	acquired, released := false, false
	deadline := time.Now().Add(time.Duration(secs) * time.Second)
	t0 := time.Now()
	for time.Now().Before(deadline) {
		ans := fetchHold(port, key)
		act := s.step(time.Now(), ans, acLine())
		el := time.Since(t0).Round(time.Second)
		switch act {
		case holdAcquire:
			acquired = true
			t.Logf("%6s acquire (server asks to keep the PC awake)", el)
		case holdRelease:
			released = true
			t.Logf("%6s release", el)
		case holdRearm:
			t.Logf("%6s re-arm", el)
		}
		if !ans.ok && !ans.gone {
			t.Logf("%6s no answer", el)
		}
		if acquired && released {
			break
		}
		time.Sleep(powerPoll)
	}
	if !acquired || !released {
		t.Fatalf("acquired=%v released=%v within %ds", acquired, released, secs)
	}
}
