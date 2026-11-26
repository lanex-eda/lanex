// Copyright 2026 LanEx Contributors
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0

// Everything this launcher does *to* Windows and WSL: listing distros, starting
// the server, shutting the appliance down, and opening the app window.
package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
	"unicode/utf16"

	"golang.org/x/sys/windows/registry"
)

// wsl.exe lives in System32. A 32-bit process would be redirected to SysWOW64,
// which has no wsl.exe — hence the x64-only build (see lanex.iss's
// ArchitecturesAllowed). Bare name, resolved on PATH, so an ARM64 host finds its
// own copy too.
const wslExe = "wsl.exe"

// ---------------------------------------------------------------- distro list --

// wslListTimeout bounds `wsl -l -q`. The first wsl.exe call after a Windows
// start can take several seconds while the WSL service starts; a minute means
// WSL itself is stuck, and the user must hear that rather than see nothing.
const wslListTimeout = 60 * time.Second

// errWslUnresponsive: wsl.exe did not answer, as opposed to "not registered".
var errWslUnresponsive = errors.New("wsl.exe did not answer")

// distroPresent reports whether the LanEx appliance distro is registered. An
// error means WSL itself failed or hung, which is a different problem (and a
// different message) from an appliance that is gone.
func distroPresent() (bool, error) {
	out, err := wslOutput(wslListTimeout, "-l", "-q")
	if err != nil {
		logf("wsl -l -q failed: %v", err)
		return false, err
	}
	for _, name := range decodeDistroList(out) {
		if strings.EqualFold(name, distroName) {
			return true, nil
		}
	}
	return false, nil
}

// wslOutput runs wsl.exe with *args* and returns its raw (undecoded) stdout,
// killing it after *timeout*.
func wslOutput(timeout time.Duration, args ...string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, wslExe, args...)
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: createNoWindow}
	// WSL_UTF8 makes modern wsl.exe emit UTF-8 instead of UTF-16LE. Older builds
	// ignore it, which is why decodeConsole still sniffs the encoding.
	cmd.Env = append(os.Environ(), "WSL_UTF8=1")
	var stdout bytes.Buffer
	cmd.Stdout = &stdout
	err := cmd.Run()
	if ctx.Err() == context.DeadlineExceeded {
		return stdout.Bytes(), fmt.Errorf("%w within %s", errWslUnresponsive, timeout)
	}
	return stdout.Bytes(), err
}

// decodeDistroList turns `wsl -l -q` output into distro names.
func decodeDistroList(raw []byte) []string {
	var names []string
	for _, line := range strings.Split(decodeConsole(raw), "\n") {
		// Trailing \r from the console, and NULs when a UTF-16 stream was cut at
		// an odd byte, both survive TrimSpace unless named explicitly.
		line = strings.TrimSpace(strings.Trim(line, "\r\x00"))
		if line != "" {
			names = append(names, line)
		}
	}
	return names
}

// decodeConsole decodes wsl.exe output, which is UTF-16LE unless WSL_UTF8=1 was
// honoured.
//
// This is the classic silent bug in every home-grown WSL launcher: comparing
// "lanex" against "l\x00a\x00n\x00e\x00x\x00", concluding the distro is missing,
// and telling the user to reinstall a perfectly good install. Hence the unit
// tests in wsl_test.go.
func decodeConsole(raw []byte) string {
	switch {
	case len(raw) >= 2 && raw[0] == 0xFF && raw[1] == 0xFE: // UTF-16LE BOM
		return decodeUTF16LE(raw[2:])
	case len(raw) >= 3 && raw[0] == 0xEF && raw[1] == 0xBB && raw[2] == 0xBF: // UTF-8 BOM
		return string(raw[3:])
	case bytes.IndexByte(raw, 0) >= 0:
		// No BOM. Valid UTF-8 never contains a zero byte, while UTF-16LE ASCII is
		// every other byte NUL — so one NUL anywhere settles it.
		return decodeUTF16LE(raw)
	default:
		return string(raw)
	}
}

func decodeUTF16LE(b []byte) string {
	if len(b)%2 != 0 {
		b = b[:len(b)-1] // truncated stream: drop the dangling byte
	}
	units := make([]uint16, len(b)/2)
	for i := range units {
		units[i] = uint16(b[2*i]) | uint16(b[2*i+1])<<8
	}
	return string(utf16.Decode(units))
}

// -------------------------------------------------------------- server start --

// startServer starts LanEx inside the appliance and returns the running child.
//
// THE ONE `wsl.exe` INVOCATION. Three rules, all learned the hard way and
// documented in docs/windows-manual.md — break any of them and the desktop
// viewers (GTKWave, KLayout, GDS3D, OpenROAD GUI) open blank windows titled
// "[WARN: COPY MODE]":
//
//  1. `bash -ic`, never `bash -c`. WSLg only brings its GUI bridge up cleanly
//     for an INTERACTIVE shell.
//  2. Exactly ONE wsl.exe command. A second, separate invocation can block the
//     first from ever running and leaves WSLg half-initialised.
//  3. No LANEX_HW_GL / LIBRELANE_GUI_WSL_HW_GL. LanEx already selects safe
//     software GL on WSL; forcing hardware GL deadlocks a degraded vGPU bridge.
//  4. X11 only, for the server and everything it starts. The appliance runs
//     systemd, which gives the user a fresh /run/user/<uid> WITHOUT WSLg's
//     wayland-0 socket, while WSL still exports WAYLAND_DISPLAY=wayland-0. A
//     toolkit or Mesa EGL that trusts that variable lands on WSLg's Wayland
//     path, which is the one that degrades to the blank taskbar-only window.
//     Removing WAYLAND_DISPLAY and pinning GTK/Qt to X11 here covers every
//     child process (native tools, container launches, anything added later),
//     not only the launch sites that remember to call wsl_gui_env.
//
// `cd ~` first, and it is not cosmetic either. wsl.exe translates the CALLING
// process's Windows working directory into the Linux one, and this launcher is
// started from its own install dir — so without the cd the server runs with
// cwd = /mnt/c/Program Files/LanEx: a directory the appliance user cannot write
// and a slow 9p mount. Everything that defaults to the current directory then
// lands there ("Could not copy the SPM example: [Errno 13] Permission denied:
// '/mnt/c/Program Files/LanEx/spm_example'", the file picker's "Current dir").
// `;` not `&&`: a cd that somehow fails must not stop LanEx from starting.
//
// `exec lanex` replaces the shell, so the process tree stays wsl.exe -> lanex
// and killing the child is unambiguous. The Windows launcher owns the initial
// window: `--no-browser` prevents the Linux process from declaring success after
// merely spawning Edge, before Windows has proved that /api/health is reachable.
// tray.waitReady opens exactly one app window only after that Windows-side probe.
func startServer(sw *startWin) (*exec.Cmd, error) {
	cmd := hiddenCmd(wslExe, startServerArgs()...)
	cmd.Env = serverEnv(os.Environ())
	// The appliance's stdout/stderr is the only diagnostic that exists when a
	// launch fails, and the failure dialogs point the user at this file. The
	// writer also watches for the two lines that move the start window on.
	if f, err := openLogFile(); err == nil {
		mw := &markerWriter{out: f, sw: sw}
		cmd.Stdout, cmd.Stderr = mw, mw
	} else {
		logf("could not open the log file: %v", err)
	}
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	return cmd, nil
}

// startKeepalive gives the distro a client again when the server inside is
// still running but the wsl.exe that started it has gone: without one, WSL
// stops an idle distro, server and all.
func startKeepalive() (*exec.Cmd, error) {
	cmd := hiddenCmd(wslExe, "-d", distroName, "-u", appUser, "--", "sleep", "infinity")
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	return cmd, nil
}

// The lines the start window waits for: the first comes from bash (the distro
// is up), the second from lanex once its server has bound its port
// (cli.py _print_url). PYTHONUNBUFFERED makes the second arrive at once
// instead of when Python's output buffer fills.
const (
	markerLinux  = "LANEX-START linux"
	markerServer = "LanEx is running at"
)

func startServerArgs() []string {
	return []string{"-d", distroName, "-u", appUser, "--", "env",
		"-u", "WAYLAND_DISPLAY",
		"GDK_BACKEND=x11",
		"QT_QPA_PLATFORM=xcb",
		"PYTHONUNBUFFERED=1",
		"LANEX_INSTANCE_ID=" + activeConfig.InstallID,
		"LANEX_SOURCE_SHA=" + activeConfig.SourceSHA,
		"LANEX_MANIFEST_HASH=" + activeConfig.Manifest,
		"bash", "-ic",
		"printf '" + markerLinux + "\\n'; cd ~ 2>/dev/null; exec lanex --no-browser"}
}

// serverEnv hands the server the Windows profile and app-data folders as
// Linux paths (WSLENV's /p), so LanEx finds .wslconfig and its network
// snapshot even where WSL's interop is switched off.
func serverEnv(env []string) []string {
	out := make([]string, 0, len(env)+1)
	wslenv := ""
	for _, kv := range env {
		if strings.HasPrefix(strings.ToUpper(kv), "WSLENV=") {
			wslenv = kv[len("WSLENV="):]
			continue
		}
		out = append(out, kv)
	}
	for _, add := range []string{"USERPROFILE/p", "LOCALAPPDATA/p"} {
		name := strings.SplitN(add, "/", 2)[0]
		has := false
		for _, part := range strings.Split(wslenv, ":") {
			if strings.EqualFold(strings.SplitN(part, "/", 2)[0], name) {
				has = true
			}
		}
		if !has {
			if wslenv != "" {
				wslenv += ":"
			}
			wslenv += add
		}
	}
	return append(out, "WSLENV="+wslenv)
}

// markerWriter passes the server's output to launcher.log and moves the start
// window on when it sees the marker lines.
type markerWriter struct {
	out     io.Writer
	sw      *startWin
	pending []byte
	seen    int
}

func (m *markerWriter) Write(p []byte) (int, error) {
	n, err := m.out.Write(p)
	if m.seen < 2 {
		m.pending = append(m.pending, p...)
		if len(m.pending) > 64<<10 {
			m.pending = m.pending[len(m.pending)-4096:]
		}
		text := string(m.pending)
		if m.seen < 1 && strings.Contains(text, markerLinux) {
			m.seen = 1
			m.sw.step(1)
		}
		if m.seen < 2 && strings.Contains(text, markerServer) {
			m.seen = 2
			m.sw.step(2)
			m.pending = nil
		}
	}
	return n, err
}

// terminateDistro shuts the appliance VM down. Killing wsl.exe on the Windows
// side does NOT stop the Linux process it started — the server would keep
// running headless — so this is what "Quit LanEx" has to end with. It also
// returns the VM's RAM to Windows.
//
// Scoped to our own distro by name: the user's other distros are never touched,
// which is the promise the whole installer is built on (never `wsl --shutdown`).
func terminateDistro() {
	if _, err := wslOutput(wslListTimeout, "--terminate", distroName); err != nil {
		logf("wsl --terminate %s: %v", distroName, err)
	}
}

// -------------------------------------------------------------- app window   --

// openAppWindow opens the cockpit in a chromeless browser window.
//
// Only used for "re-open" paths (a second click on the icon, the tray's Open
// LanEx). A normal start leaves this to LanEx itself, which does the same thing
// from inside WSL over the interop bridge (appwindow.py:272-303).
func openAppWindow(port int) error {
	args := []string{
		// With the LanEx key from server.json (probe.go appURL): the server
		// answers every /api/* request only for a page that has it.
		"--app=" + appURL(port),
		// The very profile LanEx uses for its own window (appwindow.py:246-279),
		// so both routes produce ONE taskbar identity and neither drags LanEx
		// into the user's browsing session.
		"--user-data-dir=" + appProfileDir(),
		// Same geometry as the WSL-side launch (appwindow._window_flags): the
		// cockpit is a dense multi-pane IDE, so it opens maximized — a re-open
		// from the tray must not land in a small window when a normal start
		// would be maximized.
		// Edge/Chrome ignore this for --app windows once the profile has a saved
		// placement, so maximizeNewAppWindow enforces it after the window shows.
		"--start-maximized",
		"--window-size=1440,900",
		// A fresh dedicated profile must not open the browser's first-run
		// welcome page or default-browser prompt next to the LanEx window
		// (the WSL-side launch has always passed these, appwindow.build_app_argv).
		"--no-first-run",
		"--no-default-browser-check",
	}
	before := chromiumWindows()
	var err error
	if exe := chromiumPath(); exe != "" {
		err = hiddenCmd(exe, args...).Start()
	} else {
		// Nothing found on disk (unusual — Edge ships with Windows): let the shell
		// resolve msedge through the App Paths registry key. The empty "" argument is
		// start's title parameter; without it start would eat the first quoted arg.
		err = hiddenCmd("cmd.exe", append([]string{"/c", "start", "", "msedge"}, args...)...).Start()
	}
	if err == nil {
		go maximizeNewAppWindow(before, port)
	}
	return err
}

// appProfileDir mirrors appwindow.py:250,279 exactly — including the lowercase
// "lanex" (the app-window profile predates this installer's %LOCALAPPDATA%\LanEx
// install root, and the uninstaller removes both).
func appProfileDir() string {
	return filepath.Join(os.Getenv("LOCALAPPDATA"), installDir, "app-profile", activeConfig.InstallID)
}

// Edge first: it ships with every Windows 10/11, so this practically always
// resolves — the same ordering, for the same reason, as appwindow.py:96-102.
var browserExeNames = []string{"msedge.exe", "chrome.exe"}

var browserExeSuffixes = []string{
	`Microsoft\Edge\Application\msedge.exe`,
	`Google\Chrome\Application\chrome.exe`,
}

func chromiumPath() string {
	for _, name := range browserExeNames {
		if p := appPathsLookup(name); p != "" {
			return p
		}
	}
	// Registry-less fallback: the well-known install roots. LOCALAPPDATA covers
	// per-user Chrome installs, which corporate machines often have.
	for _, suffix := range browserExeSuffixes {
		for _, root := range []string{
			os.Getenv("ProgramFiles(x86)"), os.Getenv("ProgramFiles"),
			os.Getenv("LOCALAPPDATA"),
		} {
			if root == "" {
				continue
			}
			p := filepath.Join(root, suffix)
			if st, err := os.Stat(p); err == nil && !st.IsDir() {
				return p
			}
		}
	}
	return ""
}

// appPathsLookup resolves an executable through the App Paths registry key —
// the mechanism `start msedge` itself uses, read directly so we can spawn the
// browser without a cmd.exe in the middle.
func appPathsLookup(exe string) string {
	const base = `SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\`
	for _, root := range []registry.Key{registry.LOCAL_MACHINE, registry.CURRENT_USER} {
		k, err := registry.OpenKey(root, base+exe, registry.QUERY_VALUE)
		if err != nil {
			continue
		}
		v, _, err := k.GetStringValue("") // the key's default value is the full path
		k.Close()
		if err != nil {
			continue
		}
		v = strings.Trim(strings.TrimSpace(v), `"`)
		if st, err := os.Stat(v); err == nil && !st.IsDir() {
			return v
		}
	}
	return ""
}

// openShell opens an interactive shell inside the appliance, in a VISIBLE console.
//
// The one place the appliance's isolation works against the user: a tool that
// genuinely needs a shell (a hand-rolled apt package, a git clone LanEx has no
// button for) is unreachable, because the whole installer's promise is that no
// terminal ever appears. So: on demand, never on startup. A console kept open in
// the background would be a window users close and then believe the app died,
// and it would inherit this process's Windows cwd (`C:\Program Files\LanEx`)
// anyway — hence the `cd ~`, which lands the user next to their own designs.
//
// Deliberately NOT hiddenCmd: this is the one child that must show its window.
// `wsl.exe` is a console program, so CREATE_NEW_CONSOLE gives it one of its own.
//
// `bash -lic 'cd ~; exec bash -i'` rather than the tidier `wsl --cd ~`: --cd
// needs WSL 0.51+, and an unsupported flag would greet the user with
// "Invalid command line option" instead of a shell. This form works on every
// WSL2 build. It IS a second wsl.exe invocation, which startServer's rule 2
// forbids — that rule is about the cold-boot race, where a second invocation
// can stall the first and half-initialise WSLg. This one only ever runs on a
// menu click, long after the server answered /api/health.
func openShell() (*exec.Cmd, error) {
	cmd := exec.Command(wslExe, "-d", distroName, "--",
		"bash", "-lic", "cd ~ 2>/dev/null; exec bash -i")
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: createNewConsole}
	return cmd, cmd.Start()
}

// openProjectFiles shows the user's designs in File Explorer.
//
// Small feature, large trust payoff: it proves the projects are real files on
// their own PC, not something locked inside a black box. \\wsl.localhost is the
// modern share name (\\wsl$ is the pre-2021 alias).
//
// It opens the folder where New project saves, by the same rule as Setup's
// Start-menu "LanEx Project Files" entry (lanex.iss DistroProjectPath) and the
// server's platform_env.projects_dir: %USERPROFILE%\LanEx-Projects, or
// ~/lanex-projects inside the distro when the Windows profile path has a space.
func openProjectFiles() {
	// explorer.exe exits non-zero even on success, so its error is not a signal.
	_ = hiddenCmd("explorer.exe", projectFilesPath()).Run()
}

func projectFilesPath() string {
	if home := os.Getenv("USERPROFILE"); home != "" && !strings.ContainsAny(home, " \t") {
		dir := filepath.Join(home, "LanEx-Projects")
		if err := os.MkdirAll(dir, 0o755); err == nil {
			return dir
		}
	}
	linuxHome := filepath.Join(`\\wsl.localhost\`+distroName, "home", appUser)
	if st, err := os.Stat(filepath.Join(linuxHome, "lanex-projects")); err == nil && st.IsDir() {
		return filepath.Join(linuxHome, "lanex-projects")
	}
	return linuxHome
}
