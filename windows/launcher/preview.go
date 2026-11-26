// Copyright 2026 LanEx Contributors
// Licensed under the Apache License, Version 2.0.

package main

// `LanEx.exe --preview-start-window <steps|slow|failure|missing|wsl> <out.png>`
// shows the start window in one of its states, saves a picture of it and
// exits. It touches nothing else (no WSL, no server, no settings): it exists so
// the window can be checked at every Windows scale without breaking a real
// install, and for the release's screenshots.

import (
	"image"
	"image/color"
	"image/png"
	"os"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

var (
	procGetWindowRect = user32.NewProc("GetWindowRect")
	procPrintWindow   = user32.NewProc("PrintWindow")
	procGetDC         = user32.NewProc("GetDC")
	procReleaseDC     = user32.NewProc("ReleaseDC")
	procGetDIBits     = gdi32.NewProc("GetDIBits")
)

func previewStartWindow(mode, out string) int {
	sw := startWindowBegin()
	time.Sleep(startShowAfter + 600*time.Millisecond)
	switch mode {
	case "slow":
		sw.model.mu.Lock()
		sw.model.begin = time.Now().Add(-52 * time.Second)
		sw.model.stepBegin = sw.model.begin
		sw.model.mu.Unlock()
	case "failure":
		go sw.fail(startFail{kind: failTimeout, title: "LanEx did not start",
			body: "Windows could not reach LanEx's server after 5 minutes. Your projects are safe; " +
				"nothing was changed.\n\nTry again. If it fails again, run Repair LanEx: it keeps your " +
				"projects and your other WSL distributions."})
	case "missing":
		go sw.fail(startFail{kind: failMissing, title: "LanEx's environment is missing",
			body: "Windows no longer lists LanEx's Linux environment, so LanEx cannot start.\n\n" +
				"Repair LanEx sets it up again. Projects in your LanEx-Projects folder are untouched, " +
				"and your other WSL distributions are not changed."})
	case "wsl":
		go sw.fail(startFail{kind: failWSL, title: "Windows' Linux (WSL) is not responding",
			body: "LanEx cannot start because WSL did not answer (wsl.exe did not answer within 1m0s).\n\n" +
				"Restart Windows, then open LanEx again. If it keeps happening, run Repair LanEx from the " +
				"Start menu; it keeps your projects, your PDKs and your other WSL distributions."})
	default:
		sw.model.mu.Lock()
		sw.model.begin = time.Now().Add(-17 * time.Second)
		sw.model.stepBegin = sw.model.begin
		sw.model.mu.Unlock()
		sw.model.advance(1, time.Now().Add(-6*time.Second))
		sw.model.advance(2, time.Now().Add(-4*time.Second))
	}
	sw.post(wmStartRefresh)
	time.Sleep(1500 * time.Millisecond)
	code := 0
	if err := captureWindow(sw.hwnd, out); err != nil {
		logf("preview capture: %v", err)
		code = 1
	}
	return code
}

// captureWindow saves what the window shows (DWM-rendered, title bar too).
func captureWindow(hwnd windows.HWND, out string) error {
	var r winRect
	procGetWindowRect.Call(uintptr(hwnd), uintptr(unsafe.Pointer(&r)))
	w, h := r.Right-r.Left, r.Bottom-r.Top
	screen, _, _ := procGetDC.Call(0)
	defer procReleaseDC.Call(0, screen)
	mem, _, _ := procCreateCompatibleDC.Call(screen)
	defer procDeleteDC.Call(mem)
	bmp, _, _ := procCreateCompatibleBitmap.Call(screen, uintptr(w), uintptr(h))
	defer procDeleteObject.Call(bmp)
	old, _, _ := procSelectObject.Call(mem, bmp)
	procPrintWindow.Call(uintptr(hwnd), mem, 2 /*PW_RENDERFULLCONTENT*/)
	procSelectObject.Call(mem, old)
	type bitmapInfoHeader struct {
		Size          uint32
		Width, Height int32
		Planes, Bits  uint16
		Compression   uint32
		SizeImage     uint32
		XPels, YPels  int32
		ClrUsed       uint32
		ClrImportant  uint32
	}
	bi := bitmapInfoHeader{Width: w, Height: -h, Planes: 1, Bits: 32}
	bi.Size = uint32(unsafe.Sizeof(bi))
	buf := make([]byte, int(w)*int(h)*4)
	procGetDIBits.Call(mem, bmp, 0, uintptr(h), uintptr(unsafe.Pointer(&buf[0])), uintptr(unsafe.Pointer(&bi)), 0)
	img := image.NewRGBA(image.Rect(0, 0, int(w), int(h)))
	for i := 0; i < int(w)*int(h); i++ {
		img.Set(i%int(w), i/int(w), color.RGBA{buf[i*4+2], buf[i*4+1], buf[i*4], 255})
	}
	f, err := os.Create(out)
	if err != nil {
		return err
	}
	defer f.Close()
	return png.Encode(f, img)
}
