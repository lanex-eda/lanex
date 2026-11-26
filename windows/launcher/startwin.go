// Copyright 2026 LanEx Contributors
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0

package main

// The "Starting LanEx" window.
//
// A first start after Windows restarts boots the WSL VM, then systemd and
// Docker, then Python: up to a minute or two, and on a slow PC right after a
// restart up to four (the rig once measured 4 min 40 s before Windows gave the
// VM its network). The tray tooltip used to be the only sign of life. This is
// a small native window (no WebView: Microsoft advises plain Win32 for splash
// screens) in the LanEx look: near-black blue surface, the layer-colour
// hairline, a square mark per step, a determinate bar weighted by how long
// each step usually takes, and a failure page with the log path and what to
// do next.
//
// Steps come from real signals, never a timer:
//  1. Starting Windows' Linux (WSL): until the distro prints its first line.
//  2. Starting LanEx's environment: until the server prints its address.
//  3. Starting the design server: until Windows reaches /api/health.
//  4. Opening the window: until the Edge app window shows.
//
// It appears only when the start takes longer than 800 ms, closes when the
// LanEx window shows, and closing it while starting only hides it (start-up
// carries on in the tray). Text is in real STATIC and BUTTON controls so
// Narrator reads it; Tab moves between buttons, Enter runs the first one, Esc
// hides the window (or quits on the failure page).

import (
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

// ------------------------------------------------------------------ the model

type startStep struct {
	label  string
	weight float64
	guess  time.Duration
}

var startSteps = [4]startStep{
	{"STARTING WINDOWS' LINUX (WSL)", 0.45, 20 * time.Second},
	{"STARTING LANEX'S ENVIRONMENT", 0.15, 5 * time.Second},
	{"STARTING THE DESIGN SERVER", 0.30, 10 * time.Second},
	{"OPENING THE WINDOW", 0.10, 3 * time.Second},
}

const (
	startShowAfter = 800 * time.Millisecond
	startSlowAfter = 45 * time.Second
	// While the window is up the start may take this long before LanEx gives up.
	startBudget = 5 * time.Minute
)

type failKind int

const (
	failNone failKind = iota
	failTimeout
	failExited
	failWSL
	failMissing
	failStart
)

type startFail struct {
	kind  failKind
	title string
	body  string
}

// Failure-page actions (also the button control IDs).
const (
	actTryAgain = 101 + iota
	actRepair
	actCopyDiag
	actOpenLog
	actQuit
	actHide
	actKeepWaiting
)

var actLabel = map[int]string{
	actTryAgain: "Try again", actRepair: "Repair LanEx...", actCopyDiag: "Copy diagnostics",
	actOpenLog: "Open log", actQuit: "Quit", actHide: "Hide", actKeepWaiting: "Keep waiting",
}

// startModel is what the window shows. Pure apart from the lock: the progress
// rules are unit-tested (startwin_test.go).
type startModel struct {
	mu        sync.Mutex
	begin     time.Time
	cur       int // the active step; 4 = all done
	stepBegin time.Time
	took      [4]time.Duration
	expect    [4]time.Duration
	shown     float64 // never goes back
	fail      *startFail
	copied    bool
}

func newStartModel(now time.Time, expect [4]time.Duration) *startModel {
	m := &startModel{begin: now, stepBegin: now, expect: expect}
	for i := range m.expect {
		if m.expect[i] <= 0 {
			m.expect[i] = startSteps[i].guess
		}
	}
	return m
}

// advance marks every step before *to* done.
func (m *startModel) advance(to int, now time.Time) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if to > 4 {
		to = 4
	}
	for m.cur < to {
		m.took[m.cur] = now.Sub(m.stepBegin)
		m.cur++
		m.stepBegin = now
	}
}

// progress is the bar's fill: finished steps by weight, plus the active step
// creeping toward 90% of its share by how long it usually takes. Never moves
// back and never reaches 1 before the last step is done.
func (m *startModel) progress(now time.Time) float64 {
	m.mu.Lock()
	defer m.mu.Unlock()
	p := 0.0
	for i := 0; i < m.cur && i < 4; i++ {
		p += startSteps[i].weight
	}
	if m.cur < 4 {
		el := now.Sub(m.stepBegin).Seconds()
		exp := m.expect[m.cur].Seconds()
		frac := 0.9 * (1 - math.Exp(-el/math.Max(exp, 0.5)))
		p += startSteps[m.cur].weight * frac
		if p > 0.99 {
			p = 0.99
		}
	} else {
		p = 1
	}
	if p < m.shown {
		p = m.shown
	}
	m.shown = p
	return p
}

// slow: stuck on the WSL or environment step for a while (a cold VM after a
// restart waits for its network adapter).
func (m *startModel) slow(now time.Time) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.fail == nil && m.cur < 2 && now.Sub(m.stepBegin) >= startSlowAfter
}

func (m *startModel) snapshot() (cur int, took [4]time.Duration, stepBegin, begin time.Time, f *startFail, copied bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.cur, m.took, m.stepBegin, m.begin, m.fail, m.copied
}

func fmtClock(d time.Duration) string {
	s := int(d.Round(time.Second).Seconds())
	return fmt.Sprintf("%d:%02d", s/60, s%60)
}

func fmtSecs(d time.Duration) string {
	s := int(d.Round(time.Second).Seconds())
	if s < 60 {
		return fmt.Sprintf("%d s", s)
	}
	return fmtClock(d)
}

// ------------------------------------------------- start times (for the bar)

func startTimesPath() string {
	return filepath.Join(os.Getenv("LOCALAPPDATA"), installDir, "state", "start-times.json")
}

// loadExpected: the median of the last five starts, per step.
func loadExpected() [4]time.Duration {
	var out [4]time.Duration
	raw, err := os.ReadFile(startTimesPath())
	if err != nil {
		return out
	}
	var hist [][4]float64
	if json.Unmarshal(raw, &hist) != nil {
		return out
	}
	return medianTimes(hist)
}

func medianTimes(hist [][4]float64) [4]time.Duration {
	var out [4]time.Duration
	if len(hist) == 0 {
		return out
	}
	for i := 0; i < 4; i++ {
		vals := make([]float64, 0, len(hist))
		for _, h := range hist {
			if h[i] > 0 {
				vals = append(vals, h[i])
			}
		}
		if len(vals) == 0 {
			continue
		}
		sort.Float64s(vals)
		out[i] = time.Duration(vals[len(vals)/2] * float64(time.Second))
	}
	return out
}

func saveStartTimes(took [4]time.Duration) {
	var hist [][4]float64
	if raw, err := os.ReadFile(startTimesPath()); err == nil {
		_ = json.Unmarshal(raw, &hist)
	}
	var row [4]float64
	for i, d := range took {
		row[i] = d.Seconds()
	}
	hist = append(hist, row)
	if len(hist) > 5 {
		hist = hist[len(hist)-5:]
	}
	if b, err := json.Marshal(hist); err == nil {
		_ = os.MkdirAll(filepath.Dir(startTimesPath()), 0o755)
		_ = os.WriteFile(startTimesPath(), b, 0o644)
	}
}

// ----------------------------------------------------------------- the window

var (
	gdi32                      = windows.NewLazySystemDLL("gdi32.dll")
	dwmapi                     = windows.NewLazySystemDLL("dwmapi.dll")
	procRegisterClassEx        = user32.NewProc("RegisterClassExW")
	procCreateWindowEx         = user32.NewProc("CreateWindowExW")
	procDefWindowProc          = user32.NewProc("DefWindowProcW")
	procGetMessage             = user32.NewProc("GetMessageW")
	procTranslateMessage       = user32.NewProc("TranslateMessage")
	procDispatchMessage        = user32.NewProc("DispatchMessageW")
	procIsDialogMessage        = user32.NewProc("IsDialogMessageW")
	procPostMessage            = user32.NewProc("PostMessageW")
	procSendMessage            = user32.NewProc("SendMessageW")
	procPostQuitMessage        = user32.NewProc("PostQuitMessage")
	procSetWindowText          = user32.NewProc("SetWindowTextW")
	procGetClientRect          = user32.NewProc("GetClientRect")
	procInvalidateRect         = user32.NewProc("InvalidateRect")
	procBeginPaint             = user32.NewProc("BeginPaint")
	procEndPaint               = user32.NewProc("EndPaint")
	procFillRect               = user32.NewProc("FillRect")
	procSetTimer               = user32.NewProc("SetTimer")
	procLoadCursor             = user32.NewProc("LoadCursorW")
	procDestroyWindow          = user32.NewProc("DestroyWindow")
	procSetWindowPos           = user32.NewProc("SetWindowPos")
	procMoveWindow             = user32.NewProc("MoveWindow")
	procGetDpiForWindow        = user32.NewProc("GetDpiForWindow")
	procSetForegroundWindow    = user32.NewProc("SetForegroundWindow")
	procGetCursorPos           = user32.NewProc("GetCursorPos")
	procMonitorFromPoint       = user32.NewProc("MonitorFromPoint")
	procGetMonitorInfo         = user32.NewProc("GetMonitorInfoW")
	procDrawText               = user32.NewProc("DrawTextW")
	procGetTextExtentPoint32   = gdi32.NewProc("GetTextExtentPoint32W")
	procSetFocus               = user32.NewProc("SetFocus")
	procIsWindowVisibleStart   = user32.NewProc("IsWindowVisible")
	procAdjustWindowRectEx     = user32.NewProc("AdjustWindowRectEx")
	procCreateSolidBrush       = gdi32.NewProc("CreateSolidBrush")
	procDeleteObject           = gdi32.NewProc("DeleteObject")
	procSetTextColor           = gdi32.NewProc("SetTextColor")
	procSetBkColor             = gdi32.NewProc("SetBkColor")
	procSetBkMode              = gdi32.NewProc("SetBkMode")
	procCreateFont             = gdi32.NewProc("CreateFontW")
	procSelectObject           = gdi32.NewProc("SelectObject")
	procCreateCompatibleDC     = gdi32.NewProc("CreateCompatibleDC")
	procCreateCompatibleBitmap = gdi32.NewProc("CreateCompatibleBitmap")
	procBitBlt                 = gdi32.NewProc("BitBlt")
	procDeleteDC               = gdi32.NewProc("DeleteDC")
	procDwmSetWindowAttribute  = dwmapi.NewProc("DwmSetWindowAttribute")
)

const (
	wmDestroy         = 0x0002
	wmSize            = 0x0005
	wmPaint           = 0x000F
	wmClose           = 0x0010
	wmEraseBkgnd      = 0x0014
	wmSetFont         = 0x0030
	wmDrawItem        = 0x002B
	wmCommand         = 0x0111
	wmTimer           = 0x0113
	wmCtlColorStatic  = 0x0138
	wmCtlColorBtn     = 0x0135
	wmDpiChanged      = 0x02E0
	wmApp             = 0x8000
	wmStartRefresh    = wmApp + 1
	wmStartShow       = wmApp + 2
	wmStartClose      = wmApp + 3
	wsOverlapped      = 0x00000000
	wsCaption         = 0x00C00000
	wsSysMenu         = 0x00080000
	wsMinimizeBox     = 0x00020000
	wsChild           = 0x40000000
	wsClipChildren    = 0x02000000
	wsVisible         = 0x10000000
	wsTabStop         = 0x00010000
	wsExAppWindow     = 0x00040000
	wsExControlParent = 0x00010000
	ssLeft            = 0x0000
	ssRight           = 0x0002
	ssNoPrefix        = 0x0080
	ssEndEllipsis     = 0x4000
	bsOwnerDraw       = 0x000B
	esReadOnly        = 0x0800
	esAutoHScroll     = 0x0080
	swHide            = 0
	swShowNormal      = 1
	swpNoZOrder       = 0x0004
	swpNoActivate     = 0x0010
	idOK              = 1
	idCancel          = 2
	odsFocus          = 0x0010
	odsSelected       = 0x0001
	dtCenter          = 0x0001
	dtVCenter         = 0x0004
	dtSingleLine      = 0x0020
	transparentBk     = 1
	srcCopy           = 0x00CC0020
)

type wndClassEx struct {
	Size       uint32
	Style      uint32
	WndProc    uintptr
	ClsExtra   int32
	WndExtra   int32
	Instance   windows.Handle
	Icon       windows.Handle
	Cursor     windows.Handle
	Background windows.Handle
	MenuName   *uint16
	ClassName  *uint16
	IconSm     windows.Handle
}

type winRect struct{ Left, Top, Right, Bottom int32 }

type winMsg struct {
	Hwnd    windows.HWND
	Message uint32
	WParam  uintptr
	LParam  uintptr
	Time    uint32
	Pt      struct{ X, Y int32 }
}

type paintStruct struct {
	Hdc         uintptr
	Erase       int32
	RcPaint     winRect
	Restore     int32
	IncUpdate   int32
	RgbReserved [32]byte
}

type drawItemStruct struct {
	CtlType    uint32
	CtlID      uint32
	ItemID     uint32
	ItemAction uint32
	ItemState  uint32
	HwndItem   windows.HWND
	Hdc        uintptr
	RcItem     winRect
	ItemData   uintptr
}

type monitorInfo struct {
	Size    uint32
	Monitor winRect
	Work    winRect
	Flags   uint32
}

func rgb(r, g, b uint8) uint32 { return uint32(r) | uint32(g)<<8 | uint32(b)<<16 }

// The LanEx tokens (static/styles.css, dark theme).
var (
	colBg      = rgb(0x06, 0x08, 0x0d)
	colSurface = rgb(0x0e, 0x13, 0x1c)
	colSurf2   = rgb(0x13, 0x1b, 0x28)
	colBorder  = rgb(0x1f, 0x2a, 0x3b)
	colText    = rgb(0xc2, 0xce, 0xe0)
	colStrong  = rgb(0xf2, 0xf7, 0xff)
	colMuted   = rgb(0x7e, 0x8e, 0xa7)
	colAccent  = rgb(0x2f, 0x6f, 0xe0)
	colMet1    = rgb(0x4d, 0xa3, 0xff)
	colPass    = rgb(0x2f, 0xaa, 0x6a)
	colWarn    = rgb(0xc9, 0x8a, 0x14)
	colFail    = rgb(0xe5, 0x48, 0x4d)
	// The top bar's hairline: poly, li, met1..met5.
	colLayers = []uint32{rgb(0xd6, 0x44, 0x6f), rgb(0x8e, 0xa6, 0xc4), rgb(0x4d, 0xa3, 0xff),
		rgb(0xe8, 0x79, 0xc9), rgb(0x36, 0xd3, 0x99), rgb(0xf2, 0xc1, 0x4e), rgb(0xff, 0x7a, 0x45)}
)

// Layout in DIP (96 DPI); scaled by the window's DPI.
const (
	swWidth   = 584
	swHeight  = 344
	swPad     = 28
	swStepTop = 112
	swStepH   = 28
	swBarTop  = 238
	swBtnTop  = 292
	swBtnH    = 32
)

type startWin struct {
	model   *startModel
	hwnd    windows.HWND
	created chan struct{}
	choice  chan int
	visible atomic.Bool
	closed  atomic.Bool
	hidden  atomic.Bool // the person closed it while starting

	// window-thread state
	dpi                                    uint32
	fontTitle, fontBody, fontMono, fontBtn uintptr
	brushBg                                uintptr
	title, sub, status, elapsed            windows.HWND
	stepLbl, stepTime                      [4]windows.HWND
	logEdit                                windows.HWND
	buttons                                []windows.HWND
	btnIDs                                 []int
}

var (
	activeStart *startWin
	startWinMu  sync.Mutex
	startCB     uintptr
)

// startWindowBegin creates the window on its own OS thread (fyne/systray owns
// the main one) and shows it once the start has taken startShowAfter.
func startWindowBegin() *startWin {
	w := &startWin{model: newStartModel(time.Now(), loadExpected()), created: make(chan struct{}),
		choice: make(chan int, 4)}
	startWinMu.Lock()
	activeStart = w
	startWinMu.Unlock()
	go w.run()
	go func() {
		time.Sleep(startShowAfter)
		if !w.closed.Load() {
			<-w.created
			procPostMessage.Call(uintptr(w.hwnd), wmStartShow, 0, 0)
		}
	}()
	return w
}

func (w *startWin) post(msg uint32) {
	select {
	case <-w.created:
	default:
		return
	}
	if w.hwnd != 0 && !w.closed.Load() {
		procPostMessage.Call(uintptr(w.hwnd), uintptr(msg), 0, 0)
	}
}

// step marks the steps before *i* done (0-based: step(1) = WSL is up).
func (w *startWin) step(i int) {
	if w == nil {
		return
	}
	w.model.advance(i, time.Now())
	w.post(wmStartRefresh)
}

// done: the LanEx window is open. Records the step times and closes.
func (w *startWin) done() {
	if w == nil || w.closed.Load() {
		return
	}
	w.model.advance(4, time.Now())
	cur, took, _, _, f, _ := w.model.snapshot()
	if cur == 4 && f == nil {
		saveStartTimes(took)
	}
	w.post(wmStartClose)
	w.closed.Store(true)
}

// fail shows the failure page and waits for what the person chooses: one of
// the act* constants. Without a window (it never got created) it falls back
// to a message box.
func (w *startWin) fail(f startFail) int {
	if w == nil {
		return failByMessageBox(f)
	}
	select {
	case <-w.created:
	case <-time.After(3 * time.Second):
		return failByMessageBox(f)
	}
	if w.hwnd == 0 {
		return failByMessageBox(f)
	}
	w.model.mu.Lock()
	w.model.fail = &f
	w.model.mu.Unlock()
	w.closed.Store(false)
	procPostMessage.Call(uintptr(w.hwnd), wmStartShow, 1, 0)
	w.post(wmStartRefresh)
	for {
		a := <-w.choice
		switch a {
		case actCopyDiag:
			go func() {
				path, err := copyDiagnostics()
				w.model.mu.Lock()
				w.model.copied = err == nil
				w.model.mu.Unlock()
				w.post(wmStartRefresh)
				if err != nil {
					logf("copy diagnostics: %v", err)
				} else {
					logf("diagnostics copied and saved to %s", path)
				}
			}()
		case actOpenLog:
			openURL(logPath())
		case actHide, actKeepWaiting:
			continue
		default:
			w.closed.Store(true)
			w.post(wmStartClose)
			return a
		}
	}
}

func failByMessageBox(f startFail) int {
	text := f.title + "\n\n" + f.body + "\n\nDetails are in:\n" + logPath()
	switch f.kind {
	case failMissing:
		if askYesNo(text + "\n\nRun Repair now?") {
			return actRepair
		}
		return actQuit
	default:
		showError(text)
		return actQuit
	}
}

// buttonsFor: what the failure page offers, the default first.
func buttonsFor(k failKind) []int {
	switch k {
	case failMissing:
		return []int{actRepair, actOpenLog, actQuit}
	case failWSL:
		return []int{actCopyDiag, actOpenLog, actQuit}
	case failNone:
		return []int{actHide}
	default:
		return []int{actTryAgain, actRepair, actCopyDiag, actOpenLog, actQuit}
	}
}

func (w *startWin) scale(v int) int32 {
	dpi := w.dpi
	if dpi == 0 {
		dpi = 96
	}
	return int32(math.Round(float64(v) * float64(dpi) / 96))
}

func mkFont(height int32, weight int32, face string) uintptr {
	f, _ := windows.UTF16PtrFromString(face)
	h, _, _ := procCreateFont.Call(uintptr(uint32(-height)), 0, 0, 0, uintptr(weight), 0, 0, 0,
		1 /*DEFAULT_CHARSET*/, 0, 0, 5 /*CLEARTYPE_QUALITY*/, 0, uintptr(unsafe.Pointer(f)))
	return h
}

func (w *startWin) makeFonts() {
	for _, f := range []uintptr{w.fontTitle, w.fontBody, w.fontMono, w.fontBtn} {
		if f != 0 {
			procDeleteObject.Call(f)
		}
	}
	// Bahnschrift ships with Windows 10 and 11 and reads like Barlow (both
	// DIN-style); Consolas stands in for JetBrains Mono.
	w.fontTitle = mkFont(w.scale(26), 600, "Bahnschrift SemiBold")
	w.fontBody = mkFont(w.scale(15), 400, "Segoe UI")
	w.fontMono = mkFont(w.scale(12), 400, "Consolas")
	w.fontBtn = mkFont(w.scale(14), 600, "Segoe UI Semibold")
}

func (w *startWin) child(class string, text string, style uint32, id int, font uintptr) windows.HWND {
	c, _ := windows.UTF16PtrFromString(class)
	t, _ := windows.UTF16PtrFromString(text)
	h, _, _ := procCreateWindowEx.Call(0, uintptr(unsafe.Pointer(c)), uintptr(unsafe.Pointer(t)),
		uintptr(wsChild|wsVisible|style), 0, 0, 10, 10, uintptr(w.hwnd), uintptr(id), 0, 0)
	if font != 0 {
		procSendMessage.Call(h, wmSetFont, font, 1)
	}
	return windows.HWND(h)
}

func setText(h windows.HWND, s string) {
	p, _ := windows.UTF16PtrFromString(s)
	procSetWindowText.Call(uintptr(h), uintptr(unsafe.Pointer(p)))
}

func move(h windows.HWND, x, y, wd, ht int32) {
	procMoveWindow.Call(uintptr(h), uintptr(x), uintptr(y), uintptr(wd), uintptr(ht), 1)
}

func (w *startWin) run() {
	runtime.LockOSThread()
	defer close(w.choice)
	startWinMu.Lock()
	if startCB == 0 {
		startCB = syscall.NewCallback(startWndProc)
	}
	startWinMu.Unlock()
	cls, _ := windows.UTF16PtrFromString("LanExStartWindow")
	cursor, _, _ := procLoadCursor.Call(0, 32512 /*IDC_ARROW*/)
	brush, _, _ := procCreateSolidBrush.Call(uintptr(colBg))
	w.brushBg = brush
	wc := wndClassEx{WndProc: startCB, Cursor: windows.Handle(cursor), Background: windows.Handle(brush),
		ClassName: cls}
	wc.Size = uint32(unsafe.Sizeof(wc))
	procRegisterClassEx.Call(uintptr(unsafe.Pointer(&wc)))
	title, _ := windows.UTF16PtrFromString("Starting LanEx")
	// WS_CLIPCHILDREN: the double-buffered paint must never draw over the text controls.
	style := uint32(wsOverlapped | wsCaption | wsSysMenu | wsMinimizeBox | wsClipChildren)
	h, _, err := procCreateWindowEx.Call(wsExAppWindow|wsExControlParent, uintptr(unsafe.Pointer(cls)),
		uintptr(unsafe.Pointer(title)), uintptr(style), 0, 0, 100, 100, 0, 0, 0, 0)
	if h == 0 {
		logf("start window: CreateWindowEx failed: %v", err)
		close(w.created)
		return
	}
	w.hwnd = windows.HWND(h)
	dark := int32(1)
	procDwmSetWindowAttribute.Call(h, 20 /*DWMWA_USE_IMMERSIVE_DARK_MODE*/, uintptr(unsafe.Pointer(&dark)), 4)
	// Windows 11: the title bar in the LanEx surface colour, whatever accent
	// colour the person chose for other apps (Windows 10 ignores these).
	caption, captionText := colBg, colText
	procDwmSetWindowAttribute.Call(h, 35 /*DWMWA_CAPTION_COLOR*/, uintptr(unsafe.Pointer(&caption)), 4)
	procDwmSetWindowAttribute.Call(h, 36 /*DWMWA_TEXT_COLOR*/, uintptr(unsafe.Pointer(&captionText)), 4)
	w.dpi = 96
	if procGetDpiForWindow.Find() == nil {
		if d, _, _ := procGetDpiForWindow.Call(h); d != 0 {
			w.dpi = uint32(d)
		}
	}
	w.makeFonts()
	w.title = w.child("STATIC", "Starting LanEx", ssNoPrefix, 0, w.fontTitle)
	w.sub = w.child("STATIC", "", ssNoPrefix, 0, w.fontBody)
	for i := 0; i < 4; i++ {
		w.stepLbl[i] = w.child("STATIC", startSteps[i].label, ssNoPrefix|ssEndEllipsis, 0, w.fontMono)
		w.stepTime[i] = w.child("STATIC", "", ssNoPrefix|ssRight, 0, w.fontMono)
	}
	w.status = w.child("STATIC", "", ssNoPrefix, 0, w.fontMono)
	w.elapsed = w.child("STATIC", "", ssNoPrefix|ssRight, 0, w.fontMono)
	w.logEdit = w.child("EDIT", logPath(), esReadOnly|esAutoHScroll|wsTabStop, 0, w.fontMono)
	procShowWindow.Call(uintptr(w.logEdit), swHide)
	w.placeWindow()
	w.layout()
	w.refresh()
	procSetTimer.Call(h, 1, 250, 0)
	close(w.created)

	var msg winMsg
	for {
		r, _, _ := procGetMessage.Call(uintptr(unsafe.Pointer(&msg)), 0, 0, 0)
		if int32(r) <= 0 {
			break
		}
		if r2, _, _ := procIsDialogMessage.Call(h, uintptr(unsafe.Pointer(&msg))); r2 != 0 {
			continue
		}
		procTranslateMessage.Call(uintptr(unsafe.Pointer(&msg)))
		procDispatchMessage.Call(uintptr(unsafe.Pointer(&msg)))
	}
}

// placeWindow sizes the window for its DPI and centres it on the monitor under
// the mouse (where the person just clicked LanEx).
func (w *startWin) placeWindow() {
	rc := winRect{0, 0, w.scale(swWidth), w.scale(swHeight)}
	procAdjustWindowRectEx.Call(uintptr(unsafe.Pointer(&rc)), uintptr(wsOverlapped|wsCaption|wsSysMenu|wsMinimizeBox), 0, wsExAppWindow)
	wd, ht := rc.Right-rc.Left, rc.Bottom-rc.Top
	var pt struct{ X, Y int32 }
	procGetCursorPos.Call(uintptr(unsafe.Pointer(&pt)))
	mon, _, _ := procMonitorFromPoint.Call(uintptr(uint32(pt.X))|uintptr(uint32(pt.Y))<<32, 2 /*NEAREST*/)
	mi := monitorInfo{}
	mi.Size = uint32(unsafe.Sizeof(mi))
	x, y := int32(100), int32(100)
	if r, _, _ := procGetMonitorInfo.Call(mon, uintptr(unsafe.Pointer(&mi))); r != 0 {
		x = mi.Work.Left + (mi.Work.Right-mi.Work.Left-wd)/2
		y = mi.Work.Top + (mi.Work.Bottom-mi.Work.Top-ht)/2
	}
	procSetWindowPos.Call(uintptr(w.hwnd), 0, uintptr(x), uintptr(y), uintptr(wd), uintptr(ht), swpNoZOrder|swpNoActivate)
}

func (w *startWin) layout() {
	s := w.scale
	cw := s(swWidth) - 2*s(swPad)
	move(w.title, s(swPad), s(26), cw, s(36))
	move(w.sub, s(swPad), s(64), cw, s(42))
	for i := 0; i < 4; i++ {
		y := s(swStepTop + i*swStepH)
		move(w.stepLbl[i], s(swPad+22), y, cw-s(22+80), s(20))
		move(w.stepTime[i], s(swWidth-swPad-80), y, s(80), s(20))
	}
	move(w.status, s(swPad), s(swBarTop+14), s(300), s(20))
	move(w.elapsed, s(swWidth-swPad-120), s(swBarTop+14), s(120), s(20))
	move(w.logEdit, s(swPad+36), s(swBarTop+12), cw-s(36), s(22))
	w.layoutButtons()
}

// textWidth measures *s* in the button font (pixels at the window's DPI).
func (w *startWin) textWidth(s string) int32 {
	hdc, _, _ := procGetDC.Call(uintptr(w.hwnd))
	defer procReleaseDC.Call(uintptr(w.hwnd), hdc)
	old, _, _ := procSelectObject.Call(hdc, w.fontBtn)
	defer procSelectObject.Call(hdc, old)
	u, _ := windows.UTF16FromString(s)
	var sz struct{ CX, CY int32 }
	procGetTextExtentPoint32.Call(hdc, uintptr(unsafe.Pointer(&u[0])), uintptr(len(u)-1), uintptr(unsafe.Pointer(&sz)))
	return sz.CX
}

func (w *startWin) layoutButtons() {
	s := w.scale
	x := s(swWidth - swPad)
	for i := len(w.buttons) - 1; i >= 0; i-- {
		lbl := actLabel[w.btnIDs[i]]
		bw := w.textWidth(lbl) + s(28)
		if bw < s(76) {
			bw = s(76)
		}
		x -= bw
		move(w.buttons[i], x, s(swBtnTop), bw, s(swBtnH))
		x -= s(8)
	}
}

// setButtons replaces the button row (a page change).
func (w *startWin) setButtons(ids []int) {
	same := len(ids) == len(w.btnIDs)
	for i := 0; same && i < len(ids); i++ {
		same = ids[i] == w.btnIDs[i]
	}
	if same {
		return
	}
	for _, b := range w.buttons {
		procDestroyWindow.Call(uintptr(b))
	}
	w.buttons = nil
	w.btnIDs = nil
	for _, id := range ids {
		b := w.child("BUTTON", actLabel[id], bsOwnerDraw|wsTabStop, id, w.fontBtn)
		w.buttons = append(w.buttons, b)
		w.btnIDs = append(w.btnIDs, id)
	}
	w.layoutButtons()
	if len(w.buttons) > 0 {
		procSetFocus.Call(uintptr(w.buttons[0]))
	}
}

// refresh copies the model into the controls (window thread only).
func (w *startWin) refresh() {
	now := time.Now()
	cur, took, stepBegin, begin, f, copied := w.model.snapshot()
	if f != nil {
		setText(w.title, f.title)
		setText(w.hwnd, f.title)
		setText(w.sub, f.body)
		move(w.sub, w.scale(swPad), w.scale(64), w.scale(swWidth)-2*w.scale(swPad), w.scale(swBarTop-64-8))
		for i := 0; i < 4; i++ {
			procShowWindow.Call(uintptr(w.stepLbl[i]), swHide)
			procShowWindow.Call(uintptr(w.stepTime[i]), swHide)
		}
		procShowWindow.Call(uintptr(w.elapsed), swHide)
		setText(w.status, "Log:")
		move(w.status, w.scale(swPad), w.scale(swBarTop+14), w.scale(36), w.scale(20))
		procShowWindow.Call(uintptr(w.logEdit), swShowNormal)
		w.setButtons(buttonsFor(f.kind))
		for i, id := range w.btnIDs {
			if id == actCopyDiag {
				if copied {
					setText(w.buttons[i], "Copied")
				}
			}
		}
		procInvalidateRect.Call(uintptr(w.hwnd), 0, 1)
		return
	}
	slow := w.model.slow(now)
	if slow {
		setText(w.sub, "Still starting. Right after Windows starts, Linux can take a few minutes "+
			"to get its network ready. LanEx keeps waiting.")
		w.setButtons([]int{actKeepWaiting, actQuit})
	} else {
		setText(w.sub, "The first start after Windows restarts takes up to a minute. "+
			"Your projects stay where they are.")
		w.setButtons([]int{actHide})
	}
	for i := 0; i < 4; i++ {
		switch {
		case i < cur:
			setText(w.stepTime[i], fmtSecs(took[i]))
		case i == cur:
			setText(w.stepTime[i], fmtSecs(now.Sub(stepBegin)))
		default:
			setText(w.stepTime[i], "")
		}
	}
	n := cur + 1
	if n > 4 {
		n = 4
	}
	setText(w.status, fmt.Sprintf("Step %d of 4", n))
	setText(w.elapsed, fmtClock(now.Sub(begin)))
	procInvalidateRect.Call(uintptr(w.hwnd), 0, 0)
}

func fillRect(hdc uintptr, x, y, wd, ht int32, color uint32) {
	b, _, _ := procCreateSolidBrush.Call(uintptr(color))
	rc := winRect{x, y, x + wd, y + ht}
	procFillRect.Call(hdc, uintptr(unsafe.Pointer(&rc)), b)
	procDeleteObject.Call(b)
}

func mix(a, b uint32, t float64) uint32 {
	ch := func(sh uint) uint32 {
		x := float64((a>>sh)&0xff)*(1-t) + float64((b>>sh)&0xff)*t
		return uint32(math.Round(x)) << sh
	}
	return ch(0) | ch(8) | ch(16)
}

// paint draws what controls don't: the hairline, the step marks and the bar.
func (w *startWin) paint(hdc uintptr) {
	var rc winRect
	procGetClientRect.Call(uintptr(w.hwnd), uintptr(unsafe.Pointer(&rc)))
	s := w.scale
	fillRect(hdc, 0, 0, rc.Right, rc.Bottom, colBg)
	// The layer-colour hairline (opacity 0.55 over the background).
	n := int32(len(colLayers))
	for i := int32(0); i < n; i++ {
		x0 := rc.Right * i / n
		x1 := rc.Right * (i + 1) / n
		fillRect(hdc, x0, 0, x1-x0, s(2), mix(colBg, colLayers[i], 0.55))
	}
	cur, _, _, _, f, _ := w.model.snapshot()
	now := time.Now()
	if f == nil {
		for i := 0; i < 4; i++ {
			y := s(swStepTop+i*swStepH) + s(4)
			x := s(swPad + 2)
			sq := s(10)
			switch {
			case i < cur:
				fillRect(hdc, x, y, sq, sq, colPass)
			case i == cur:
				// The active step: met1 with a soft glow, like the rail's via.
				fillRect(hdc, x-s(3), y-s(3), sq+s(6), sq+s(6), mix(colBg, colMet1, 0.18))
				fillRect(hdc, x-s(1), y-s(1), sq+s(2), sq+s(2), mix(colBg, colMet1, 0.40))
				fillRect(hdc, x, y, sq, sq, colMet1)
			default:
				fillRect(hdc, x, y, sq, sq, colBorder)
				fillRect(hdc, x+s(1), y+s(1), sq-2*s(1), sq-2*s(1), colBg)
			}
		}
	}
	// The bar: a 4 px track, an accent-to-met1 fill; red on failure, amber while slow.
	bx, by, bw, bh := s(swPad), s(swBarTop), rc.Right-2*s(swPad), s(4)
	fillRect(hdc, bx, by, bw, bh, colSurf2)
	if f != nil {
		fillRect(hdc, bx, by, bw, bh, colFail)
		return
	}
	p := w.model.progress(now)
	fw := int32(float64(bw) * p)
	end := colMet1
	if w.model.slow(now) {
		end = colWarn
	}
	for x := int32(0); x < fw; x += 2 {
		fillRect(hdc, bx+x, by, 2, bh, mix(colAccent, end, float64(x)/float64(bw)))
	}
}

func (w *startWin) drawButton(di *drawItemStruct) {
	hdc := di.Hdc
	r := di.RcItem
	primary := len(w.btnIDs) > 0 && int(di.CtlID) == w.btnIDs[0]
	bg, fg, border := colSurf2, colText, colBorder
	if primary {
		bg, fg, border = colAccent, colStrong, colMet1
	}
	if di.ItemState&odsSelected != 0 {
		bg = mix(bg, colBg, 0.3)
	}
	fillRect(hdc, r.Left, r.Top, r.Right-r.Left, r.Bottom-r.Top, border)
	fillRect(hdc, r.Left+1, r.Top+1, r.Right-r.Left-2, r.Bottom-r.Top-2, bg)
	if di.ItemState&odsFocus != 0 {
		// A visible focus ring in met1, inside the border.
		fillRect(hdc, r.Left+2, r.Top+2, r.Right-r.Left-4, 1, colMet1)
		fillRect(hdc, r.Left+2, r.Bottom-3, r.Right-r.Left-4, 1, colMet1)
		fillRect(hdc, r.Left+2, r.Top+2, 1, r.Bottom-r.Top-4, colMet1)
		fillRect(hdc, r.Right-3, r.Top+2, 1, r.Bottom-r.Top-4, colMet1)
	}
	buf := make([]uint16, 128)
	n, _, _ := procGetWindowText.Call(uintptr(di.HwndItem), uintptr(unsafe.Pointer(&buf[0])), 128)
	procSetBkMode.Call(hdc, transparentBk)
	procSetTextColor.Call(hdc, uintptr(fg))
	procSelectObject.Call(hdc, w.fontBtn)
	tr := r
	procDrawText.Call(hdc, uintptr(unsafe.Pointer(&buf[0])), n, uintptr(unsafe.Pointer(&tr)), dtCenter|dtVCenter|dtSingleLine)
}

func (w *startWin) onCommand(id int) {
	switch id {
	case idOK:
		if len(w.btnIDs) > 0 {
			id = w.btnIDs[0]
		}
	case idCancel:
		_, _, _, _, f, _ := w.model.snapshot()
		if f != nil {
			id = actQuit
		} else {
			id = actHide
		}
	}
	switch id {
	case actHide:
		w.hide()
	case actKeepWaiting:
		w.model.mu.Lock()
		w.model.stepBegin = time.Now()
		w.model.mu.Unlock()
		w.refresh()
	case actQuit:
		_, _, _, _, f, _ := w.model.snapshot()
		if f == nil {
			// Quitting a start in progress: the same as the tray's Quit.
			logf("start window: Quit chosen while starting")
			go func() { quitDuringStart() }()
			w.hide()
			return
		}
		select {
		case w.choice <- id:
		default:
		}
	default:
		select {
		case w.choice <- id:
		default:
		}
	}
}

func (w *startWin) hide() {
	procShowWindow.Call(uintptr(w.hwnd), swHide)
	w.visible.Store(false)
	w.hidden.Store(true)
	setTrayTip(appName + " is still starting; its window opens when it is ready")
}

func startWndProc(hwnd windows.HWND, msg uint32, wp, lp uintptr) uintptr {
	startWinMu.Lock()
	w := activeStart
	startWinMu.Unlock()
	if w == nil || (w.hwnd != 0 && w.hwnd != hwnd) {
		r, _, _ := procDefWindowProc.Call(uintptr(hwnd), uintptr(msg), wp, lp)
		return r
	}
	switch msg {
	case wmEraseBkgnd:
		return 1
	case wmPaint:
		var ps paintStruct
		hdc, _, _ := procBeginPaint.Call(uintptr(hwnd), uintptr(unsafe.Pointer(&ps)))
		var rc winRect
		procGetClientRect.Call(uintptr(hwnd), uintptr(unsafe.Pointer(&rc)))
		mem, _, _ := procCreateCompatibleDC.Call(hdc)
		bmp, _, _ := procCreateCompatibleBitmap.Call(hdc, uintptr(rc.Right), uintptr(rc.Bottom))
		old, _, _ := procSelectObject.Call(mem, bmp)
		w.paint(mem)
		procBitBlt.Call(hdc, 0, 0, uintptr(rc.Right), uintptr(rc.Bottom), mem, 0, 0, srcCopy)
		procSelectObject.Call(mem, old)
		procDeleteObject.Call(bmp)
		procDeleteDC.Call(mem)
		procEndPaint.Call(uintptr(hwnd), uintptr(unsafe.Pointer(&ps)))
		return 0
	case wmCtlColorStatic:
		hdc, ctl := wp, windows.HWND(lp)
		color := colText
		switch {
		case ctl == w.title:
			color = colStrong
		case ctl == w.sub:
			color = colMuted
			if _, _, _, _, f, _ := w.model.snapshot(); f != nil {
				color = colText
			}
		case ctl == w.status || ctl == w.elapsed:
			color = colMuted
		}
		for i := 0; i < 4; i++ {
			if ctl == w.stepLbl[i] || ctl == w.stepTime[i] {
				cur, _, _, _, _, _ := w.model.snapshot()
				switch {
				case i < cur:
					color = colText
				case i == cur:
					color = colStrong
				default:
					color = colMuted
				}
			}
		}
		procSetTextColor.Call(hdc, uintptr(color))
		procSetBkColor.Call(hdc, uintptr(colBg))
		return w.brushBg
	case wmDrawItem:
		w.drawButton((*drawItemStruct)(ptrFrom(lp)))
		return 1
	case wmCommand:
		w.onCommand(int(wp & 0xffff))
		return 0
	case wmTimer:
		if procIsWindowVisibleStart.Find() == nil {
			if v, _, _ := procIsWindowVisibleStart.Call(uintptr(hwnd)); v != 0 {
				w.refresh()
			}
		}
		return 0
	case wmStartRefresh:
		w.refresh()
		return 0
	case wmStartShow:
		if wp == 0 && w.hidden.Load() {
			return 0 // the person hid it; a failure (wp=1) shows it again
		}
		procShowWindow.Call(uintptr(hwnd), swShowNormal)
		procSetForegroundWindow.Call(uintptr(hwnd))
		w.visible.Store(true)
		w.refresh()
		return 0
	case wmStartClose:
		procDestroyWindow.Call(uintptr(hwnd))
		return 0
	case wmDpiChanged:
		w.dpi = uint32(wp & 0xffff)
		w.makeFonts()
		for _, h := range []windows.HWND{w.title, w.sub, w.status, w.elapsed, w.logEdit} {
			procSendMessage.Call(uintptr(h), wmSetFont, w.fontBody, 1)
		}
		procSendMessage.Call(uintptr(w.title), wmSetFont, w.fontTitle, 1)
		for i := 0; i < 4; i++ {
			procSendMessage.Call(uintptr(w.stepLbl[i]), wmSetFont, w.fontMono, 1)
			procSendMessage.Call(uintptr(w.stepTime[i]), wmSetFont, w.fontMono, 1)
		}
		procSendMessage.Call(uintptr(w.status), wmSetFont, w.fontMono, 1)
		procSendMessage.Call(uintptr(w.elapsed), wmSetFont, w.fontMono, 1)
		procSendMessage.Call(uintptr(w.logEdit), wmSetFont, w.fontMono, 1)
		for _, b := range w.buttons {
			procSendMessage.Call(uintptr(b), wmSetFont, w.fontBtn, 1)
		}
		r := (*winRect)(ptrFrom(lp))
		procSetWindowPos.Call(uintptr(hwnd), 0, uintptr(r.Left), uintptr(r.Top), uintptr(r.Right-r.Left),
			uintptr(r.Bottom-r.Top), swpNoZOrder|swpNoActivate)
		w.layout()
		return 0
	case wmClose:
		w.onCommand(idCancel)
		return 0
	case wmDestroy:
		w.visible.Store(false)
		procPostQuitMessage.Call(0)
		return 0
	}
	r, _, _ := procDefWindowProc.Call(uintptr(hwnd), uintptr(msg), wp, lp)
	return r
}

// ------------------------------------------------------------ glue for main

// tray tooltip before the tray exists is a no-op (set by tray.go once ready).
var setTrayTip = func(string) {}

// quitDuringStart is set by the tray (it owns stopping the server).
var quitDuringStart = func() { os.Exit(0) }

// lastLogLines: the end of launcher.log, for the "stopped while starting" page.
func lastLogLines(n int) string {
	raw, err := os.ReadFile(logPath())
	if err != nil {
		return ""
	}
	lines := strings.Split(strings.TrimRight(string(raw), "\r\n"), "\n")
	var keep []string
	for i := len(lines) - 1; i >= 0 && len(keep) < n; i-- {
		l := strings.TrimSpace(lines[i])
		if l == "" || strings.HasPrefix(l, "[launcher") {
			continue
		}
		keep = append([]string{l}, keep...)
	}
	return strings.Join(keep, "\n")
}

// ptrFrom turns a pointer Windows handed over as a message parameter or a
// return value into a Go pointer. The memory is Windows' (the message's
// struct, a locked global block), never Go-managed.
func ptrFrom(p uintptr) unsafe.Pointer { return *(*unsafe.Pointer)(unsafe.Pointer(&p)) }
