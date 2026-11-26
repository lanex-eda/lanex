# Copyright 2026 LanEx Contributors
# Licensed under the Apache License, Version 2.0.
#
# network.ps1 -- what Windows knows about the network, for LanEx's Linux.
#
# On a company or university network Windows finds its proxy (often through an
# automatic setup script, a PAC file) and trusts the company's TLS-inspection
# certificate, but the Linux tools inside LanEx's WSL distro (apt, Docker, pip)
# see neither: the pre-check passes on Windows, then the install fails inside
# Linux. This script asks Windows itself, without changing anything:
#
#   * the proxy for each site LanEx downloads from, resolved by Windows' own
#     resolver ([System.Net.WebRequest]::GetSystemWebProxy: manual, PAC, WPAD);
#     HTTP(S)_PROXY set in Windows' environment wins, as before;
#   * one HEAD request per site, recording the root certificate its TLS chain
#     ends in. Validation is never weakened: the callback only reads the chain
#     and returns Windows' own verdict.
#
# It writes <OutDir>\network.json and <OutDir>\network-ca.pem (atomically) and
# never writes a password: a proxy address with user:pass@ is recorded without
# them, with a note. The PAC address is not written either. Runs unelevated
# (Setup is PrivilegesRequired=lowest, the launcher runs as the user) so the
# settings read are the person's own. Windows PowerShell 5.1.
#
# windows/provision/network-apply.py applies the result inside the distro.

param(
    [Parameter(Mandatory = $true)][string]$OutDir,
    [int]$TimeoutMs = 8000
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2

try {
    $tls = [Net.SecurityProtocolType]::Tls12
    try { $tls = $tls -bor [Net.SecurityProtocolType]12288 } catch { }   # TLS 1.3 where .NET knows it
    [Net.ServicePointManager]::SecurityProtocol = $tls
} catch { }

$Hosts = @(
    'snapshot.ubuntu.com', 'archive.ubuntu.com', 'pypi.org', 'files.pythonhosted.org',
    'ghcr.io', 'pkg-containers.githubusercontent.com', 'github.com', 'api.github.com',
    'objects.githubusercontent.com'
)

Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Net;
using System.Net.Security;
using System.Security.Cryptography.X509Certificates;
using System.Threading.Tasks;

public class LxProbe {
    public string Host = "";
    public string Proxy = "";
    public int Status;
    public string Error = "";
    public byte[] RootDer;
    public string RootSubject = "";
    public bool Trusted;
    public string ProxyAuth = "";

    public static LxProbe Run(string host, string proxy, int timeoutMs) {
        LxProbe r = new LxProbe();
        r.Host = host;
        r.Proxy = proxy;
        try {
            HttpWebRequest req = (HttpWebRequest)WebRequest.Create("https://" + host + "/");
            req.Method = "HEAD";
            req.Timeout = timeoutMs;
            req.ReadWriteTimeout = timeoutMs;
            req.AllowAutoRedirect = false;
            req.UserAgent = "LanEx-Setup-network-check";
            if (proxy == "DIRECT") { req.Proxy = null; }
            else if (!String.IsNullOrEmpty(proxy)) { req.Proxy = new WebProxy(proxy); }
            // No credentials on purpose: a proxy that needs a Windows sign-in must
            // show up here as 407, because apt, Docker and pip cannot sign in.
            req.ServerCertificateValidationCallback = delegate (object s, X509Certificate cert, X509Chain chain, SslPolicyErrors errors) {
                if (chain != null && chain.ChainElements.Count > 0) {
                    X509Certificate2 root = chain.ChainElements[chain.ChainElements.Count - 1].Certificate;
                    r.RootDer = root.RawData;
                    r.RootSubject = root.Subject;
                }
                r.Trusted = (errors == SslPolicyErrors.None);
                return errors == SslPolicyErrors.None;
            };
            using (HttpWebResponse resp = (HttpWebResponse)req.GetResponse()) {
                r.Status = (int)resp.StatusCode;
            }
        } catch (WebException ex) {
            HttpWebResponse resp = ex.Response as HttpWebResponse;
            if (resp != null) {
                r.Status = (int)resp.StatusCode;
                if (r.Status == 407) { r.ProxyAuth = resp.Headers["Proxy-Authenticate"] ?? ""; }
                resp.Close();
            } else {
                r.Error = ex.Status.ToString() + ": " + ex.Message;
            }
        } catch (Exception ex) {
            r.Error = ex.GetType().Name + ": " + ex.Message;
        }
        return r;
    }

    public static LxProbe[] RunAll(string[] hosts, string[] proxies, int timeoutMs) {
        Task<LxProbe>[] tasks = new Task<LxProbe>[hosts.Length];
        for (int i = 0; i < hosts.Length; i++) {
            string h = hosts[i]; string p = proxies[i];
            tasks[i] = Task.Factory.StartNew(() => Run(h, p, timeoutMs));
        }
        Task.WaitAll(tasks, timeoutMs * 2);
        LxProbe[] outp = new LxProbe[hosts.Length];
        for (int i = 0; i < hosts.Length; i++) {
            if (tasks[i].IsCompleted) { outp[i] = tasks[i].Result; }
            else { outp[i] = new LxProbe(); outp[i].Host = hosts[i]; outp[i].Proxy = proxies[i]; outp[i].Error = "Timeout: no answer"; }
        }
        return outp;
    }
}
'@

function Remove-UserInfo([string]$Url) {
    if (-not $Url) { return '' }
    return ($Url -replace '^([A-Za-z][A-Za-z0-9+.-]*://)[^/@]*@', '$1')
}

function Normalize-Proxy([string]$Value) {
    # "host:port", "http://host:port", or a WinINET per-protocol list piece.
    $v = ($Value + '').Trim()
    if (-not $v) { return '' }
    if ($v -notmatch '^[A-Za-z][A-Za-z0-9+.-]*://') { $v = 'http://' + $v }
    $v = Remove-UserInfo $v
    if ($v -notmatch '^https?://') { return '' }   # socks and others: apt and ciel can't use them
    if (-not $v.EndsWith('/')) { $v = $v + '/' }
    return $v
}

# WinINET's bypass list ("*.corp.com;10.*;<local>") as one NO_PROXY that curl,
# apt, Go, requests and urllib all read.
function Convert-Bypass([string]$Override) {
    $items = New-Object System.Collections.Generic.List[string]
    $dropped = $false
    foreach ($raw in (($Override + '') -split '[;\s,]+')) {
        $e = $raw.Trim()
        if (-not $e) { continue }
        if ($e -ieq '<local>') { $dropped = $true; continue }
        if ($e -match '^(\d{1,3})\.\*$') { $e = "$($Matches[1]).0.0.0/8" }
        elseif ($e -match '^(\d{1,3})\.(\d{1,3})\.\*$') { $e = "$($Matches[1]).$($Matches[2]).0.0/16" }
        elseif ($e -match '^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.\*$') { $e = "$($Matches[1]).$($Matches[2]).$($Matches[3]).0/24" }
        elseif ($e -match '^\*\.(.+)$') { $e = '.' + $Matches[1] }
        $e = $e -replace '^https?://', ''
        if ($e -match '^[A-Za-z0-9.*:/_\[\]-]+$' -and -not $items.Contains($e)) { $items.Add($e) }
    }
    foreach ($m in @('localhost', '127.0.0.1', '::1')) { if (-not $items.Contains($m)) { $items.Add($m) } }
    return @{ value = ($items -join ','); droppedLocal = $dropped }
}

function Get-WslNetworkingMode {
    try {
        $p = Join-Path $env:USERPROFILE '.wslconfig'
        if (Test-Path -LiteralPath $p) {
            $text = Get-Content -LiteralPath $p -Raw -ErrorAction Stop
            if ($text -match '(?im)^\s*networkingMode\s*=\s*(\w+)') { return $Matches[1].ToLowerInvariant() }
        }
    } catch { }
    return 'nat'
}

$problems = New-Object System.Collections.Generic.List[string]
$notes = New-Object System.Collections.Generic.List[string]

# 1. The person's settings.
$inet = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'
$ie = $null
try { $ie = Get-ItemProperty -LiteralPath $inet -ErrorAction Stop } catch { }
function IeValue($name) { if ($ie -and ($ie.PSObject.Properties.Name -contains $name)) { return $ie.$name } return $null }
$manualOn = [int](IeValue 'ProxyEnable') -eq 1
$manualServer = [string](IeValue 'ProxyServer')
$override = [string](IeValue 'ProxyOverride')
$pacSet = [bool]([string](IeValue 'AutoConfigURL'))

$envProxy = ''
foreach ($name in @('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy')) {
    $v = [Environment]::GetEnvironmentVariable($name)
    if (-not $v) { $v = [Environment]::GetEnvironmentVariable($name, 'User') }
    if ($v) { $envProxy = $v; break }
}
$envNoProxy = [Environment]::GetEnvironmentVariable('NO_PROXY')
if (-not $envNoProxy) { $envNoProxy = [Environment]::GetEnvironmentVariable('NO_PROXY', 'User') }

$mode = 'none'
if ($envProxy) { $mode = 'env' }
elseif ($pacSet) { $mode = 'pac' }
elseif ($manualOn -and $manualServer) { $mode = 'manual' }

if ($envProxy -and ($envProxy -match '://[^/@]*@')) {
    $notes.Add('Your HTTP_PROXY carries a user name or password. LanEx uses it while Setup runs and does not save it, so Docker and later downloads use the proxy without it.')
}

# 2. The proxy for each site, by Windows' own resolver.
$sys = [System.Net.WebRequest]::GetSystemWebProxy()
$perHost = [ordered]@{}
foreach ($h in $Hosts) {
    if ($mode -eq 'env') { $perHost[$h] = Normalize-Proxy $envProxy; continue }
    $u = [Uri]("https://$h/")
    $p = 'DIRECT'
    try {
        if (-not $sys.IsBypassed($u)) {
            $r = $sys.GetProxy($u)
            if ($r -and ($r.AbsoluteUri -ne $u.AbsoluteUri)) { $p = Normalize-Proxy $r.AbsoluteUri }
        }
    } catch { $p = 'DIRECT' }
    if (-not $p) { $p = 'DIRECT' }
    $perHost[$h] = $p
}
$used = @($perHost.Values | Where-Object { $_ -ne 'DIRECT' })
if ($mode -eq 'none' -and $used.Count -gt 0) { $mode = 'wpad' }   # found by Windows' automatic detection
$proxy = ''
if ($used.Count -gt 0) {
    $proxy = ($used | Group-Object | Sort-Object Count -Descending | Select-Object -First 1).Name
}
$shape = 'none'
if ($proxy) {
    $distinct = @($perHost.Values | Select-Object -Unique)
    if ($distinct.Count -eq 1) { $shape = 'uniform' }
    elseif (@($distinct | Where-Object { $_ -ne 'DIRECT' }).Count -eq 1) { $shape = 'split' }
    else { $shape = 'multi'; $notes.Add('Different sites use different proxies; LanEx uses the one most sites use.') }
}
$loopback = $false
if ($proxy -match '^https?://(127\.0\.0\.1|localhost|\[::1\])(:|/)') { $loopback = $true }

$bypass = Convert-Bypass ($(if ($mode -eq 'env') { $envNoProxy } else { $override }))
if ($bypass.droppedLocal) { $notes.Add('The "<local>" bypass (names without a dot) has no Linux equivalent and was left out.') }

# 3. Probe each site through what Windows would use.
$probeProxies = @($Hosts | ForEach-Object { if ($perHost[$_] -eq 'DIRECT') { 'DIRECT' } else { $perHost[$_] } })
$results = [LxProbe]::RunAll([string[]]$Hosts, [string[]]$probeProxies, $TimeoutMs)

$auth = ''
$certs = [ordered]@{}
$probes = @()
foreach ($r in $results) {
    $cat = ''
    if ($r.Status -eq 407) {
        $cat = 'proxy-auth'
        $a = ($r.ProxyAuth + '').ToLowerInvariant()
        if ($a -match 'negotiate|kerberos') { $auth = 'negotiate' } elseif ($a -match 'ntlm') { $auth = 'ntlm' } elseif (-not $auth) { $auth = 'basic' }
    } elseif ($r.Error) {
        if ($r.Error -match 'TrustFailure|SecureChannelFailure|certificate') { $cat = 'tls' }
        elseif ($r.Error -match 'NameResolutionFailure|ProxyNameResolutionFailure') { $cat = 'dns' }
        elseif ($r.Error -match 'Timeout') { $cat = 'timeout' }
        else { $cat = 'route' }
    }
    if ($r.RootDer -and $r.Trusted) {
        $sha = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($r.RootDer)).Replace('-', '').ToLowerInvariant()
        if (-not $certs.Contains($sha)) {
            $certs[$sha] = @{ sha256 = $sha; subject = $r.RootSubject; source = $r.Host; der = $r.RootDer }
        }
    }
    if ($cat -eq 'tls' -and $r.RootSubject) {
        $problems.Add("$($r.Host)'s certificate comes from '$($r.RootSubject)', which Windows does not trust either, so it is not a company certificate LanEx can copy. Check this PC's date and time, or ask your IT team.")
    }
    $probes += [ordered]@{ host = $r.Host; status = $r.Status; category = $cat; proxy = $(if ($r.Proxy -eq 'DIRECT') { 'DIRECT' } elseif ($r.Proxy) { 'proxy' } else { '' }); error = ($r.Error -replace '://[^/@\s]*@', '://') }
}
if ($auth) {
    if ($auth -eq 'basic') {
        $problems.Add('The proxy asks for a user name and password. LanEx does not store passwords, so apt, Docker and pip cannot use it. Ask your IT team to let the download sites through without a sign-in.')
    } else {
        $problems.Add('The proxy asks for a Windows sign-in (NTLM or Kerberos). Windows programs can do that, but the Linux tools LanEx uses (apt, Docker, pip) cannot, and LanEx never stores your password. Ask your IT team to let the download sites through without a sign-in, or run a sign-in helper such as px on this PC and use it as the proxy.')
    }
}

# 4. Write the snapshot (no password, no PAC address).
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$pem = New-Object System.Text.StringBuilder
foreach ($c in $certs.Values) {
    $b64 = [Convert]::ToBase64String($c.der)
    [void]$pem.Append("-----BEGIN CERTIFICATE-----`n")
    for ($i = 0; $i -lt $b64.Length; $i += 64) { [void]$pem.Append($b64.Substring($i, [Math]::Min(64, $b64.Length - $i)) + "`n") }
    [void]$pem.Append("-----END CERTIFICATE-----`n")
}
$snapshot = [ordered]@{
    schema = 1
    generated = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    mode = $(if ($proxy) { $mode } else { 'none' })
    proxy = [ordered]@{ http = $proxy; https = $proxy }
    noProxy = $bypass.value
    perHost = $perHost
    shape = $shape
    loopback = $loopback
    auth = $auth
    networkingMode = (Get-WslNetworkingMode)
    certificates = @($certs.Values | ForEach-Object { [ordered]@{ sha256 = $_.sha256; subject = $_.subject; source = $_.source } })
    probes = $probes
    problems = @($problems)
    notes = @($notes)
}
$utf8 = New-Object System.Text.UTF8Encoding($false)
$jsonPath = Join-Path $OutDir 'network.json'
$pemPath = Join-Path $OutDir 'network-ca.pem'
[IO.File]::WriteAllText($pemPath + '.tmp', $pem.ToString(), $utf8)
Move-Item -LiteralPath ($pemPath + '.tmp') -Destination $pemPath -Force
[IO.File]::WriteAllText($jsonPath + '.tmp', ($snapshot | ConvertTo-Json -Depth 6), $utf8)
Move-Item -LiteralPath ($jsonPath + '.tmp') -Destination $jsonPath -Force

# One line for the install log (no proxy address, no certificate names).
$what = if ($proxy) { "using the proxy Windows uses for these sites ($mode)" } else { 'direct connection, no proxy' }
Write-Output ("network: " + $what + "; " + $certs.Count + " root certificate(s) seen; " + $problems.Count + " problem(s)")
foreach ($p in $problems) { Write-Output ("network problem: " + $p) }
exit 0
