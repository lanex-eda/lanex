// Copyright 2026 LanEx Contributors
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0

package main

// "Copy diagnostics" from the tray and the start window's failure page: the
// one that works when the server will not start (the app's own button needs a
// running server). The Windows side (Windows and WSL versions, whether the
// distro is registered, the WSL settings LanEx cares about, the network
// snapshot, the install outcome, the end of launcher.log), then `lanex doctor`
// from inside the distro when it answers. The user name and proxy address are
// removed, so the text can go straight into a bug report. Saved under
// %LOCALAPPDATA%\LanEx\logs and put on the clipboard.

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

var (
	procOpenClipboard    = user32.NewProc("OpenClipboard")
	procCloseClipboard   = user32.NewProc("CloseClipboard")
	procEmptyClipboard   = user32.NewProc("EmptyClipboard")
	procSetClipboardData = user32.NewProc("SetClipboardData")
	procGlobalAlloc      = kernel32Power.NewProc("GlobalAlloc")
	procGlobalLock       = kernel32Power.NewProc("GlobalLock")
	procGlobalUnlock     = kernel32Power.NewProc("GlobalUnlock")
	procGlobalFree       = kernel32Power.NewProc("GlobalFree")
)

var (
	userPathRx  = regexp.MustCompile(`(?i)([A-Z]:\\Users\\)[^\\"\r\n]+`)
	linuxUserRx = regexp.MustCompile(`(/mnt/[a-z]/Users/)[^/\s"]+`)
	proxyRx     = regexp.MustCompile(`(?i)\bhttps?://[^\s"'/]+(:\d+)?/?`)
)

// shareText removes the Windows user name and any proxy-looking address.
func shareText(s string) string {
	s = userPathRx.ReplaceAllString(s, "${1}<user>")
	s = linuxUserRx.ReplaceAllString(s, "${1}<user>")
	s = proxyRx.ReplaceAllStringFunc(s, func(m string) string {
		low := strings.ToLower(m)
		for _, keep := range []string{"127.0.0.1", "localhost", "github.com", "ghcr.io", "ubuntu.com", "pypi.org", "pythonhosted.org"} {
			if strings.Contains(low, keep) {
				return m
			}
		}
		return "<proxy>"
	})
	return s
}

// wslconfigKeys: the [wsl2] keys that matter to LanEx, read only.
func wslconfigKeys() string {
	raw, err := os.ReadFile(filepath.Join(os.Getenv("USERPROFILE"), ".wslconfig"))
	if err != nil {
		return "no .wslconfig (Windows defaults)"
	}
	text := decodeConsole(raw)
	var out []string
	in := false
	for _, line := range strings.Split(text, "\n") {
		l := strings.TrimSpace(strings.Trim(line, "\r"+string(rune(0xFEFF))))
		if strings.HasPrefix(l, "[") {
			in = strings.EqualFold(l, "[wsl2]")
			continue
		}
		if !in || !strings.Contains(l, "=") {
			continue
		}
		k := strings.ToLower(strings.TrimSpace(strings.SplitN(l, "=", 2)[0]))
		switch k {
		case "memory", "processors", "networkingmode", "autoproxy", "swap":
			out = append(out, strings.ReplaceAll(l, " ", ""))
		}
	}
	if len(out) == 0 {
		return ".wslconfig [wsl2]: memory, processors and networkingMode not set"
	}
	return ".wslconfig [wsl2]: " + strings.Join(out, ", ")
}

func networkSummary() string {
	raw, err := os.ReadFile(filepath.Join(os.Getenv("LOCALAPPDATA"), installDir, "state", "network.json"))
	if err != nil {
		return "Network: no snapshot yet"
	}
	var snap struct {
		Mode         string   `json:"mode"`
		Shape        string   `json:"shape"`
		Auth         string   `json:"auth"`
		Certificates []any    `json:"certificates"`
		Problems     []string `json:"problems"`
		Generated    string   `json:"generated"`
	}
	if json.Unmarshal(raw, &snap) != nil {
		return "Network: snapshot unreadable"
	}
	s := fmt.Sprintf("Network: proxy mode %s (%s)", snap.Mode, snap.Shape)
	if snap.Auth != "" {
		s += ", proxy asks for a sign-in (" + snap.Auth + ")"
	}
	s += fmt.Sprintf(", %d root certificate(s) seen, taken %s", len(snap.Certificates), snap.Generated)
	for _, p := range snap.Problems {
		s += "\n  problem: " + p
	}
	return s
}

func setupOutcome() string {
	raw, err := os.ReadFile(statePath())
	if err != nil {
		return "Setup state: missing"
	}
	var st struct {
		Phase   string `json:"phase"`
		Outcome struct {
			Code    string `json:"code"`
			At      string `json:"at"`
			Message string `json:"message"`
		} `json:"outcome"`
	}
	_ = json.Unmarshal(raw, &st)
	s := "Setup state: " + st.Phase
	if st.Outcome.Code != "" {
		s += ", last outcome " + st.Outcome.Code + " " + st.Outcome.At
	}
	return s
}

func tailFile(path string, n int) string {
	raw, err := os.ReadFile(path)
	if err != nil {
		return "(not found)"
	}
	lines := strings.Split(strings.TrimRight(string(raw), "\r\n"), "\n")
	if len(lines) > n {
		lines = lines[len(lines)-n:]
	}
	return strings.Join(lines, "\n")
}

// diagnosticsText builds the share copy. Never blocks longer than about 70 s.
func diagnosticsText() string {
	var b strings.Builder
	now := time.Now()
	fmt.Fprintf(&b, "LanEx diagnostics · %s (share copy: user name and proxy address removed)\n", now.Format("2006-01-02 15:04"))
	b.WriteString("== Windows ==\n")
	v := windows.RtlGetVersion()
	fmt.Fprintf(&b, "Windows %d.%d.%d\n", v.MajorVersion, v.MinorVersion, v.BuildNumber)
	if out, err := wslOutput(10*time.Second, "--version"); err == nil {
		lines := strings.Split(strings.TrimSpace(decodeConsole(out)), "\n")
		if len(lines) > 3 {
			lines = lines[:3]
		}
		b.WriteString(strings.Join(lines, " | ") + "\n")
	} else {
		fmt.Fprintf(&b, "wsl --version: %v\n", err)
	}
	present, perr := distroPresent()
	switch {
	case perr != nil:
		fmt.Fprintf(&b, "Distro %s: WSL did not answer (%v)\n", distroName, perr)
	case present:
		fmt.Fprintf(&b, "Distro %s: registered\n", distroName)
	default:
		fmt.Fprintf(&b, "Distro %s: NOT registered\n", distroName)
	}
	b.WriteString(wslconfigKeys() + "\n")
	b.WriteString(networkSummary() + "\n")
	b.WriteString(setupOutcome() + "\n")
	fmt.Fprintf(&b, "Install log: %s\nLauncher log: %s\n",
		filepath.Join(os.Getenv("LOCALAPPDATA"), installDir, "logs", "install.log"), logPath())
	b.WriteString("\n== lanex doctor ==\n")
	if present && perr == nil {
		out, err := wslOutput(60*time.Second, "-d", distroName, "-u", appUser, "--",
			"/home/"+appUser+"/.local/bin/lanex", "doctor", "--share", "--offline", "-v")
		text := strings.TrimSpace(decodeConsole(out))
		if text == "" && err != nil {
			fmt.Fprintf(&b, "lanex doctor: %v\n", err)
		} else {
			b.WriteString(text + "\n")
		}
	} else {
		b.WriteString("not run: the LanEx environment is not available\n")
	}
	b.WriteString("\n== launcher.log (last 60 lines) ==\n")
	b.WriteString(tailFile(logPath(), 60) + "\n")
	return shareText(b.String())
}

// copyDiagnostics saves the share copy and puts it on the clipboard.
func copyDiagnostics() (string, error) {
	text := diagnosticsText()
	dir := filepath.Join(os.Getenv("LOCALAPPDATA"), installDir, "logs")
	_ = os.MkdirAll(dir, 0o755)
	path := filepath.Join(dir, "diagnostics-"+time.Now().Format("20060102-150405")+".txt")
	if err := os.WriteFile(path, []byte(strings.ReplaceAll(text, "\n", "\r\n")), 0o644); err != nil {
		path = ""
	}
	return path, setClipboard(strings.ReplaceAll(text, "\n", "\r\n"))
}

func setClipboard(text string) error {
	u, err := windows.UTF16FromString(text)
	if err != nil {
		return err
	}
	var opened uintptr
	for i := 0; i < 10; i++ { // another program may hold the clipboard for a moment
		if opened, _, _ = procOpenClipboard.Call(0); opened != 0 {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	if opened == 0 {
		return fmt.Errorf("the clipboard is busy")
	}
	defer procCloseClipboard.Call()
	procEmptyClipboard.Call()
	size := uintptr(len(u) * 2)
	h, _, err := procGlobalAlloc.Call(0x0002 /*GMEM_MOVEABLE*/, size)
	if h == 0 {
		return err
	}
	p, _, _ := procGlobalLock.Call(h)
	if p == 0 {
		procGlobalFree.Call(h)
		return fmt.Errorf("GlobalLock failed")
	}
	dst := unsafe.Slice((*uint16)(ptrFrom(p)), len(u))
	copy(dst, u)
	procGlobalUnlock.Call(h)
	if r, _, err := procSetClipboardData.Call(13 /*CF_UNICODETEXT*/, h); r == 0 {
		procGlobalFree.Call(h)
		return err
	}
	return nil
}
