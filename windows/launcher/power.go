// Copyright 2026 LanEx Contributors
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0

package main

// Keeping the PC awake while a run needs it.
//
// A chip run takes 5 to 60 minutes, and a laptop that goes to sleep in the
// middle of it pauses the WSL VM and, often, loses the run. The server knows
// when a run is going and says so at GET /api/power/hold
// ({"hold": bool, "reason": "..."}); the launcher, which is the Windows
// process, asks every 5 s and holds a power request while the answer is yes.
//
// PowerCreateRequest + PowerSetRequest(PowerRequestSystemRequired), not
// SetThreadExecutionState: a power request belongs to a handle, not to an OS
// thread, so Go moving the goroutine between threads cannot leave a hold
// behind, and its reason string shows in `powercfg /requests`. The thread
// call stays as the fallback, made from one locked OS thread only.
//
// What it never does: hold the display (that blocks idle maintenance), use
// away mode (for media apps), or stop the lid or the power button: Windows
// ends every request on a user-chosen sleep, and on battery about 5 minutes
// after its own sleep timer. After a sleep the request is set again.

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"runtime"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

const (
	powerPoll       = 5 * time.Second
	powerNoAnswer   = 15 * time.Second // three polls with no answer (lane 1 contract): let the PC sleep again
	powerResumeGap  = 15 * time.Second // a poll gap this long means the PC slept
	powerHoldReason = "LanEx: a chip design run is in progress"
)

// ------------------------------------------------------------ state machine --

type holdAnswer struct {
	ok   bool // the server answered with a usable body
	hold bool
	gone bool // 404/401/403: no hold endpoint (older server) or no access
}

type holdAction int

const (
	holdNone holdAction = iota
	holdAcquire
	holdRelease
	holdRearm
)

// holdState decides, one poll at a time, what to do with the power request.
// Pure, so the rules are unit-tested (power_test.go).
type holdState struct {
	held     bool
	lastOK   time.Time
	lastPoll time.Time
	ac       int // last AC line status seen (-1 unknown)
}

func (s *holdState) step(now time.Time, a holdAnswer, ac int) holdAction {
	now = now.Round(0)
	slept := !s.lastPoll.IsZero() && now.Sub(s.lastPoll) > powerResumeGap
	acChanged := s.ac >= 0 && ac >= 0 && ac != s.ac
	s.lastPoll = now
	if ac >= 0 {
		s.ac = ac
	}
	switch {
	case a.ok && a.hold:
		s.lastOK = now
		if !s.held {
			s.held = true
			return holdAcquire
		}
		if slept || acChanged {
			return holdRearm
		}
		return holdNone
	case a.ok || a.gone:
		s.lastOK = now
		if s.held {
			s.held = false
			return holdRelease
		}
		return holdNone
	default: // no answer: a server restart is short; a gone server is not
		if s.held && now.Sub(s.lastOK) >= powerNoAnswer {
			s.held = false
			return holdRelease
		}
		return holdNone
	}
}

// --------------------------------------------------------------- the server --

func powerHoldURL(port int) string {
	return fmt.Sprintf("http://127.0.0.1:%d/api/power/hold", port)
}

// fetchHold asks the server. The key from server.json goes in X-LanEx-Key, as
// the server takes it from scripts (server/auth.py); this endpoint is not one
// of the few that work without it.
func fetchHold(port int, key string) holdAnswer {
	req, err := http.NewRequest("GET", powerHoldURL(port), nil)
	if err != nil {
		return holdAnswer{}
	}
	if key != "" {
		req.Header.Set("X-LanEx-Key", key)
	}
	resp, err := probeClient.Do(req)
	if err != nil {
		return holdAnswer{}
	}
	defer resp.Body.Close()
	switch resp.StatusCode {
	case http.StatusNotFound, http.StatusUnauthorized, http.StatusForbidden, http.StatusMethodNotAllowed:
		return holdAnswer{gone: true}
	case http.StatusOK:
	default:
		return holdAnswer{}
	}
	return parseHold(io.LimitReader(resp.Body, 4096))
}

func parseHold(r io.Reader) holdAnswer {
	var env struct {
		OK   *bool `json:"ok"`
		Data *struct {
			Hold bool `json:"hold"`
		} `json:"data"`
		Hold *bool `json:"hold"`
	}
	if err := json.NewDecoder(r).Decode(&env); err != nil {
		return holdAnswer{}
	}
	if env.OK != nil && !*env.OK {
		return holdAnswer{}
	}
	if env.Data != nil {
		return holdAnswer{ok: true, hold: env.Data.Hold}
	}
	if env.Hold != nil {
		return holdAnswer{ok: true, hold: *env.Hold}
	}
	return holdAnswer{}
}

// ----------------------------------------------------------- Windows: hold --

var (
	kernel32Power               = windows.NewLazySystemDLL("kernel32.dll")
	procPowerCreateRequest      = kernel32Power.NewProc("PowerCreateRequest")
	procPowerSetRequest         = kernel32Power.NewProc("PowerSetRequest")
	procPowerClearRequest       = kernel32Power.NewProc("PowerClearRequest")
	procSetThreadExecutionState = kernel32Power.NewProc("SetThreadExecutionState")
	procGetSystemPowerStatus    = kernel32Power.NewProc("GetSystemPowerStatus")
)

const (
	powerRequestSystemRequired = 1
	powerRequestContextSimple  = 0x1
	esContinuous               = 0x80000000
	esSystemRequired           = 0x00000001
)

// reasonContext is REASON_CONTEXT with the SimpleReasonString arm of its union
// (32 bytes on amd64: two ULONGs and a 24-byte union).
type reasonContext struct {
	Version uint32
	Flags   uint32
	Reason  *uint16
	_       [16]byte
}

func sizeofReasonContext() uintptr { return unsafe.Sizeof(reasonContext{}) }

// powerHold holds the system awake, by power request or, if that cannot be
// created, by SetThreadExecutionState on one locked OS thread.
type powerHold struct {
	handle  windows.Handle
	reason  *uint16
	steCmds chan bool // fallback: true = hold, false = release
}

func newPowerHold() *powerHold {
	p := &powerHold{}
	reason, _ := windows.UTF16PtrFromString(powerHoldReason)
	p.reason = reason
	ctx := reasonContext{Version: 0, Flags: powerRequestContextSimple, Reason: reason}
	h, _, err := procPowerCreateRequest.Call(uintptr(unsafe.Pointer(&ctx)))
	runtime.KeepAlive(reason)
	if h != 0 && windows.Handle(h) != windows.InvalidHandle {
		p.handle = windows.Handle(h)
		return p
	}
	logf("power: PowerCreateRequest failed (%v); using SetThreadExecutionState", err)
	p.steCmds = make(chan bool)
	go func() {
		// The execution state is per thread: set and clear on the same one.
		runtime.LockOSThread()
		for on := range p.steCmds {
			if on {
				procSetThreadExecutionState.Call(esContinuous | esSystemRequired)
			} else {
				procSetThreadExecutionState.Call(esContinuous)
			}
		}
		procSetThreadExecutionState.Call(esContinuous)
	}()
	return p
}

func (p *powerHold) set() {
	if p.handle != 0 {
		if r, _, err := procPowerSetRequest.Call(uintptr(p.handle), powerRequestSystemRequired); r == 0 {
			logf("power: PowerSetRequest failed: %v", err)
		}
		return
	}
	p.steCmds <- true
}

func (p *powerHold) clear() {
	if p.handle != 0 {
		procPowerClearRequest.Call(uintptr(p.handle), powerRequestSystemRequired)
		return
	}
	p.steCmds <- false
}

func (p *powerHold) close() {
	if p.handle != 0 {
		windows.CloseHandle(p.handle)
		p.handle = 0
		return
	}
	if p.steCmds != nil {
		close(p.steCmds)
		p.steCmds = nil
	}
}

// acLine reports 1 on AC power, 0 on battery, -1 when unknown.
func acLine() int {
	var st struct {
		ACLineStatus        byte
		BatteryFlag         byte
		BatteryLifePercent  byte
		SystemStatusFlag    byte
		BatteryLifeTime     uint32
		BatteryFullLifeTime uint32
	}
	if r, _, _ := procGetSystemPowerStatus.Call(uintptr(unsafe.Pointer(&st))); r == 0 {
		return -1
	}
	switch st.ACLineStatus {
	case 0, 1:
		return int(st.ACLineStatus)
	}
	return -1
}

// watchPower polls the server and holds the PC awake while it asks. It ends,
// releasing any hold, when LanEx stops.
func (t *tray) watchPower() {
	var s holdState
	s.ac = -1
	var hold *powerHold
	defer func() {
		if hold != nil {
			if s.held {
				hold.clear()
			}
			hold.close()
		}
	}()
	loggedGone := false
	for {
		select {
		case <-t.stopped:
			return
		case <-time.After(powerPoll):
		}
		if t.quitting.Load() {
			return
		}
		port := int(t.port.Load())
		var ans holdAnswer
		if port > 0 {
			key := ""
			if rec, ok := readServerRecord(); ok && rec.port == port && keyPattern.MatchString(rec.key) {
				key = rec.key
			}
			ans = fetchHold(port, key)
		}
		if ans.gone && !loggedGone {
			logf("power: the server has no keep-awake endpoint; the PC sleeps as usual")
			loggedGone = true
		}
		switch s.step(time.Now(), ans, acLine()) {
		case holdAcquire:
			if hold == nil {
				hold = newPowerHold()
			}
			hold.set()
			logf("power: keeping this PC awake (a run is in progress)")
			t.setStatusTip(appName + " — keeping this PC awake while a run finishes")
		case holdRearm:
			if hold != nil {
				hold.clear()
				hold.set()
			}
			logf("power: set again after a sleep or a power-source change")
		case holdRelease:
			if hold != nil {
				hold.clear()
			}
			logf("power: released")
			t.setStatusTip(fmt.Sprintf("%s — running on port %d", appName, port))
		}
	}
}
