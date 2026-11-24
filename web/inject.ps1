# CodeBuddy / WorkBuddy 一键登录脚本（网页版生成，勿改占位符）
$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}
if ($PSVersionTable.PSVersion.Major -lt 5) {
    Write-Host "本脚本需要 PowerShell 5.0 以上（Windows 10 / 11 自带），当前版本过低，无法运行。" -ForegroundColor Red
    exit
}

$SESSION_B64 = '__SESSION_B64__'
$session = ([System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($SESSION_B64))) | ConvertFrom-Json
$ENDPOINT = '__ENDPOINT__'
$domain = $session.domain
if (-not $domain) { $domain = 'www.codebuddy.cn' }
$label = $session.account.label
if (-not $label) { $label = $session.account.nickname }

Write-Host ""
Write-Host "==== CodeBuddy / WorkBuddy 一键登录 ====" -ForegroundColor Cyan
Write-Host "账号: $label"
Write-Host ""

# ---------- [1/3] 检测这台电脑装了哪些软件 ----------
function Find-App {
    param([string[]]$Names, [string[]]$Paths)
    foreach ($p in $Paths) { if ($p -and (Test-Path $p)) { return $true } }
    $regRoots = @(
        'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*')
    foreach ($r in $regRoots) {
        $items = Get-ItemProperty $r -ErrorAction SilentlyContinue
        foreach ($it in $items) {
            if ($it.DisplayName) {
                foreach ($n in $Names) { if ($it.DisplayName -like "*$n*") { return $true } }
            }
        }
    }
    return $false
}

$pf = $env:ProgramFiles; if (-not $pf) { $pf = 'C:\Program Files' }
$pfx = ${env:ProgramFiles(x86)}; if (-not $pfx) { $pfx = 'C:\Program Files (x86)' }
$wbDataRoot = Join-Path $env:LOCALAPPDATA 'CodeBuddyExtension'
$wbInstalled = Find-App @('WorkBuddy') @(
    $wbDataRoot,
    (Join-Path $env:LOCALAPPDATA 'Programs\WorkBuddy'),
    (Join-Path $pf 'WorkBuddy'),
    (Join-Path $pfx 'WorkBuddy'))
$ideDirNames = @('CodeBuddy CN', 'CodeBuddy')
$ideDirs = @()
$ideStale = @()
foreach ($n in $ideDirNames) {
    $d = Join-Path $env:APPDATA $n
    if (Test-Path $d) {
        $hasLS = Test-Path (Join-Path $d 'Local State')
        $hasDb = Test-Path (Join-Path $d 'User\globalStorage\state.vscdb')
        if ($hasLS) { $ideDirs += [pscustomobject]@{ Name = $n; Dir = $d } }
        elseif ($hasDb) { $ideStale += "$n（有登录数据库但缺 Local State 密钥文件）" }
        else { $ideStale += "$n（没有登录数据文件）" }
    }
}
$ideInstalled = ($ideDirs.Count -gt 0) -or (Find-App @('CodeBuddy') @(
    (Join-Path $env:LOCALAPPDATA 'Programs\CodeBuddy'),
    (Join-Path $pf 'CodeBuddy'),
    (Join-Path $pfx 'CodeBuddy')))

Write-Host "[1/3] 检测软件安装情况" -ForegroundColor Cyan
Write-Host ("    WorkBuddy 桌面版: " + $(if ($wbInstalled) { '已安装' } else { '未安装（将跳过）' }))
Write-Host ("    CodeBuddy IDE: " + $(if ($ideDirs.Count) { '已安装（' + (($ideDirs | ForEach-Object { $_.Name }) -join '、') + '）' } elseif ($ideInstalled) { '已安装但未找到数据目录' } else { '未安装（将跳过）' }))
if ($ideStale.Count) {
    Write-Host "    忽略残留目录：$($ideStale -join '；')（不是可用的 IDE 安装）" -ForegroundColor DarkGray
}
if (-not $wbInstalled -and $ideDirs.Count -eq 0) {
    Write-Host ""
    if ($ideInstalled) {
        Write-Host "CodeBuddy IDE 好像装了但从未启动过（没有数据目录），请先打开一次 CodeBuddy 再重跑本脚本。" -ForegroundColor Yellow
    } else {
        Write-Host "这台电脑没有安装 WorkBuddy 桌面版，也没有 CodeBuddy IDE，本脚本无处可写。" -ForegroundColor Yellow
        Write-Host "请先安装其中任意一个软件，再运行本脚本。" -ForegroundColor Yellow
    }
    Write-Host ""
    exit
}

# ---------- [2/3] 关闭正在运行的客户端 ----------
Write-Host "[2/3] 关闭正在运行的客户端" -ForegroundColor Cyan
$killed = $false
$script:exePaths = @{}
foreach ($exe in @('CodeBuddy CN', 'CodeBuddy', 'WorkBuddy', 'WorkDaddy')) {
    $procs = Get-Process -Name $exe -ErrorAction SilentlyContinue
    if ($procs) {
        foreach ($p in $procs) {
            try { if ($p.Path) { $script:exePaths[$exe] = $p.Path } } catch {}
        }
        taskkill /IM "$exe.exe" /F 2>$null | Out-Null
        Write-Host "    已关闭 $exe"
        $killed = $true
    }
}
if (-not $killed) { Write-Host "    两个软件都没在运行" }
if ($killed) {
    Start-Sleep -Seconds 1
    foreach ($exe in @('CodeBuddy CN', 'CodeBuddy', 'WorkBuddy', 'WorkDaddy')) {
        if (Get-Process -Name $exe -ErrorAction SilentlyContinue) {
            Write-Host "    注意：$exe 没能关掉，请手动关闭它再重跑（不关掉写入可能失败）" -ForegroundColor Yellow
        }
    }
}

# ---------- [3/3] 续期并写入登录态 ----------
Write-Host "[3/3] 续期并写入登录态" -ForegroundColor Cyan
$auth = $session.auth
try {
    $rt = $auth.refreshToken
    if ($rt) {
        $headers = @{
            'X-Refresh-Token'       = $rt
            'X-Auth-Refresh-Source' = 'plugin'
            'X-Domain'              = $domain
            'X-No-Authorization'    = 'true'
            'X-No-User-Id'          = 'true'
            'X-No-Enterprise-Id'    = 'true'
            'X-No-Department-Info'  = 'true'
        }
        $resp = Invoke-RestMethod -Method Post -Uri "$ENDPOINT/v2/plugin/auth/token/refresh" -Headers $headers -Body '{}' -ContentType 'application/json' -UserAgent 'curl/8.0.1' -TimeoutSec 20 -UseBasicParsing
        $d = $resp.data
        if ($d -and $d.accessToken) {
            $auth.accessToken = $d.accessToken
            if ($d.refreshToken) { $auth.refreshToken = $d.refreshToken }
            if ($d.accessTokenExpiresAt) { $auth | Add-Member -NotePropertyName expiresAt -NotePropertyValue ([int64]$d.accessTokenExpiresAt) -Force }
            if ($d.refreshTokenExpiresAt) { $auth | Add-Member -NotePropertyName refreshExpiresAt -NotePropertyValue ([int64]$d.refreshTokenExpiresAt) -Force }
            $auth | Add-Member -NotePropertyName lastRefreshTime -NotePropertyValue ([int64](([DateTime]::UtcNow - [DateTime]::new(1970, 1, 1, 0, 0, 0, [DateTimeKind]::Utc)).TotalMilliseconds)) -Force
            $session.accessToken = "$($session.account.uid)+$($d.accessToken)"
            $session.refreshToken = $auth.refreshToken
            $session.token = $session.accessToken
            if ($d.accessTokenExpiresAt) { $session.expiresAt = [int64]$d.accessTokenExpiresAt }
            if ($d.refreshTokenExpiresAt) { $session.refreshExpiresAt = [int64]$d.refreshTokenExpiresAt }
            Write-Host "    续期成功，已换新 token"
        } else {
            Write-Host "    续期无新 token，改用脚本内嵌登录态" -ForegroundColor Yellow
        }
    }
} catch {
    $msg = $_.Exception.Message
    try {
        $rs = $_.Exception.Response.GetResponseStream()
        $reader = New-Object IO.StreamReader($rs)
        $bodyText = $reader.ReadToEnd()
        if ($bodyText) { $msg += " | " + $bodyText.Substring(0, [Math]::Min(200, $bodyText.Length)) }
    } catch {}
    Write-Host "    续期失败（$msg），改用脚本内嵌登录态" -ForegroundColor Yellow
}

function ConvertTo-JsonFile($obj, $path) {
    $json = $obj | ConvertTo-Json -Depth 12
    $tmp = "$path.tmp"
    [IO.File]::WriteAllText($tmp, $json, (New-Object System.Text.UTF8Encoding($false)))
    Move-Item -Force $tmp $path
}

$written = @()
$skipped = @()

# ---- WorkBuddy 桌面版 ----
if ($wbInstalled) {
    try {
        $acct = $session.account
        foreach ($kv in @(@('nickname', $acct.label), @('avatarUrl', ''), @('phoneNumber', ''), @('phoneAreaCode', ''), @('email', ''), @('enterpriseId', ''), @('enterpriseName', ''), @('departmentIds', @()), @('departmentName', ''), @('createTime', ''), @('updateTime', ''))) {
            if (-not $acct.PSObject.Properties[$kv[0]] -or $null -eq $acct.($kv[0])) {
                $acct | Add-Member -NotePropertyName $kv[0] -NotePropertyValue $kv[1] -Force
            }
        }
        $wbDir = Join-Path $wbDataRoot 'Data\Public\auth'
        $wbFile = Join-Path $wbDir 'workbuddy-desktop.info'
        $wbAuthDomain = $domain
        if (Test-Path $wbFile) {
            try {
                $old = Get-Content $wbFile -Raw -Encoding UTF8 | ConvertFrom-Json
                if ($old.auth -and $old.auth.domain) { $wbAuthDomain = $old.auth.domain }
                Copy-Item $wbFile "$wbFile.bak-$(Get-Date -Format 'yyyyMMddHHmmss')" -Force
            } catch {}
        }
        $auth | Add-Member -NotePropertyName domain -NotePropertyValue $wbAuthDomain -Force
        New-Item -ItemType Directory -Force -Path $wbDir | Out-Null
        $wbObj = [ordered]@{ account = $acct; auth = $auth; accounts = @($acct); allAccounts = @($acct) }
        ConvertTo-JsonFile $wbObj $wbFile
        $written += "WorkBuddy 桌面版（$wbFile）"
        Write-Host "    WorkBuddy 登录态已写入" -ForegroundColor Green
    } catch {
        $skipped += "WorkBuddy 桌面版（写入失败：$($_.Exception.Message)）"
        Write-Host "    WorkBuddy 写入失败：$($_.Exception.Message)" -ForegroundColor Red
    }
} else {
    $skipped += 'WorkBuddy 桌面版（未安装）'
}

# ---- CodeBuddy IDE ----
$ideReady = $true
try {
    if (-not (Test-Path "$env:windir\System32\winsqlite3.dll")) { throw "系统缺少 winsqlite3.dll（需要 Windows 10 以上）" }
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class Sqlite {
    [DllImport("winsqlite3.dll", CallingConvention=CallingConvention.Cdecl)] public static extern int sqlite3_open16([MarshalAs(UnmanagedType.LPWStr)] string f, out IntPtr db);
    [DllImport("winsqlite3.dll", CallingConvention=CallingConvention.Cdecl)] public static extern int sqlite3_close(IntPtr db);
    [DllImport("winsqlite3.dll", CallingConvention=CallingConvention.Cdecl)] public static extern int sqlite3_prepare_v2(IntPtr db, byte[] sql, int n, out IntPtr stmt, IntPtr tail);
    [DllImport("winsqlite3.dll", CallingConvention=CallingConvention.Cdecl)] public static extern int sqlite3_bind_text(IntPtr stmt, int idx, byte[] text, int n, IntPtr d);
    [DllImport("winsqlite3.dll", CallingConvention=CallingConvention.Cdecl)] public static extern int sqlite3_step(IntPtr stmt);
    [DllImport("winsqlite3.dll", CallingConvention=CallingConvention.Cdecl)] public static extern int sqlite3_finalize(IntPtr stmt);
    [DllImport("winsqlite3.dll", CallingConvention=CallingConvention.Cdecl)] public static extern int sqlite3_column_count(IntPtr stmt);
    [DllImport("winsqlite3.dll", CallingConvention=CallingConvention.Cdecl)] public static extern IntPtr sqlite3_column_text(IntPtr stmt, int col);
    public static void Exec(IntPtr db, string sql, string[] args) {
        IntPtr stmt;
        byte[] sqlb = Encoding.UTF8.GetBytes(sql + "\0");
        int rc = sqlite3_prepare_v2(db, sqlb, sqlb.Length, out stmt, IntPtr.Zero);
        if (rc != 0) throw new Exception("prepare rc=" + rc);
        try {
            for (int i = 0; i < args.Length; i++) {
                byte[] tb = Encoding.UTF8.GetBytes(args[i]);
                sqlite3_bind_text(stmt, i + 1, tb, tb.Length, new IntPtr(-1));
            }
            rc = sqlite3_step(stmt);
            if (rc != 100 && rc != 101) throw new Exception("step rc=" + rc);
        } finally { sqlite3_finalize(stmt); }
    }
    public static string Utf8ToString(IntPtr p) {
        if (p == IntPtr.Zero) return null;
        int len = 0;
        while (Marshal.ReadByte(p, len) != 0) len++;
        byte[] buf = new byte[len];
        Marshal.Copy(p, buf, 0, len);
        return Encoding.UTF8.GetString(buf);
    }
    public static System.Collections.Generic.List<string> Query1(IntPtr db, string sql) {
        var list = new System.Collections.Generic.List<string>();
        IntPtr stmt;
        byte[] sqlb = Encoding.UTF8.GetBytes(sql + "\0");
        int rc = sqlite3_prepare_v2(db, sqlb, sqlb.Length, out stmt, IntPtr.Zero);
        if (rc != 0) return list;
        try {
            while (sqlite3_step(stmt) == 100) {
                IntPtr p = sqlite3_column_text(stmt, 0);
                string s = Utf8ToString(p);
                if (s != null) list.Add(s);
            }
        } finally { sqlite3_finalize(stmt); }
        return list;
    }
}
public static class Gcm {
    [StructLayout(LayoutKind.Sequential)] public struct BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO {
        public int cbSize; public int dwInfoVersion; public IntPtr pbNonce; public int cbNonce;
        public IntPtr pbAuthData; public int cbAuthData; public IntPtr pbTag; public int cbTag;
        public IntPtr pbMacContext; public int cbMacContext; public int cbAAD; public long cbData;
        public int dwFlags;
    }
    [DllImport("bcrypt.dll")] public static extern int BCryptOpenAlgorithmProvider(out IntPtr hAlg, [MarshalAs(UnmanagedType.LPWStr)] string alg, [MarshalAs(UnmanagedType.LPWStr)] string impl, int flags);
    [DllImport("bcrypt.dll")] public static extern int BCryptSetProperty(IntPtr hObj, [MarshalAs(UnmanagedType.LPWStr)] string prop, byte[] input, int len, int flags);
    [DllImport("bcrypt.dll")] public static extern int BCryptGenerateSymmetricKey(IntPtr hAlg, out IntPtr hKey, IntPtr pbKeyObj, int cbKeyObj, byte[] secret, int len, int flags);
    [DllImport("bcrypt.dll")] public static extern int BCryptEncrypt(IntPtr hKey, byte[] input, int inLen, ref BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO info, byte[] iv, int ivLen, byte[] output, int outLen, out int result, int flags);
    [DllImport("bcrypt.dll")] public static extern int BCryptDecrypt(IntPtr hKey, byte[] input, int inLen, ref BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO info, byte[] iv, int ivLen, byte[] output, int outLen, out int result, int flags);
    [DllImport("bcrypt.dll")] public static extern int BCryptDestroyKey(IntPtr hKey);
    [DllImport("bcrypt.dll")] public static extern int BCryptCloseAlgorithmProvider(IntPtr hAlg, int flags);
    public static byte[] Encrypt(byte[] key, byte[] iv, byte[] plain) {
        IntPtr hAlg, hKey;
        int rc = BCryptOpenAlgorithmProvider(out hAlg, "AES", null, 0);
        if (rc != 0) throw new Exception("open alg rc=" + rc);
        try {
            rc = BCryptSetProperty(hAlg, "ChainingMode", Encoding.Unicode.GetBytes("ChainingModeGCM\0"), 34, 0);
            if (rc != 0) throw new Exception("set prop rc=" + rc);
            rc = BCryptGenerateSymmetricKey(hAlg, out hKey, IntPtr.Zero, 0, key, key.Length, 0);
            if (rc != 0) throw new Exception("gen key rc=" + rc);
            try {
                byte[] tag = new byte[16];
                var info = new BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO();
                info.cbSize = Marshal.SizeOf(typeof(BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO));
                info.dwInfoVersion = 1;
                info.pbNonce = Marshal.AllocHGlobal(iv.Length);
                Marshal.Copy(iv, 0, info.pbNonce, iv.Length);
                info.cbNonce = iv.Length;
                info.pbTag = Marshal.AllocHGlobal(16);
                info.cbTag = 16;
                byte[] outBuf = new byte[plain.Length];
                int result;
                byte[] ivCopy = (byte[])iv.Clone();
                rc = BCryptEncrypt(hKey, plain, plain.Length, ref info, ivCopy, ivCopy.Length, outBuf, outBuf.Length, out result, 0);
                Marshal.Copy(info.pbTag, tag, 0, 16);
                Marshal.FreeHGlobal(info.pbNonce);
                Marshal.FreeHGlobal(info.pbTag);
                if (rc != 0) throw new Exception("encrypt rc=" + rc);
                byte[] all = new byte[outBuf.Length + 16];
                Array.Copy(outBuf, all, outBuf.Length);
                Array.Copy(tag, 0, all, outBuf.Length, 16);
                return all;
            } finally { BCryptDestroyKey(hKey); }
        } finally { BCryptCloseAlgorithmProvider(hAlg, 0); }
    }
    public static byte[] Decrypt(byte[] key, byte[] iv, byte[] ctAndTag) {
        if (ctAndTag.Length < 16) throw new Exception("data too short");
        byte[] tag = new byte[16];
        Array.Copy(ctAndTag, ctAndTag.Length - 16, tag, 0, 16);
        byte[] body = new byte[ctAndTag.Length - 16];
        Array.Copy(ctAndTag, 0, body, 0, body.Length);
        IntPtr hAlg, hKey;
        int rc = BCryptOpenAlgorithmProvider(out hAlg, "AES", null, 0);
        if (rc != 0) throw new Exception("open alg rc=" + rc);
        try {
            rc = BCryptSetProperty(hAlg, "ChainingMode", Encoding.Unicode.GetBytes("ChainingModeGCM\0"), 34, 0);
            if (rc != 0) throw new Exception("set prop rc=" + rc);
            rc = BCryptGenerateSymmetricKey(hAlg, out hKey, IntPtr.Zero, 0, key, key.Length, 0);
            if (rc != 0) throw new Exception("gen key rc=" + rc);
            try {
                var info = new BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO();
                info.cbSize = Marshal.SizeOf(typeof(BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO));
                info.dwInfoVersion = 1;
                info.pbNonce = Marshal.AllocHGlobal(iv.Length);
                Marshal.Copy(iv, 0, info.pbNonce, iv.Length);
                info.cbNonce = iv.Length;
                info.pbTag = Marshal.AllocHGlobal(16);
                Marshal.Copy(tag, 0, info.pbTag, 16);
                info.cbTag = 16;
                byte[] outBuf = new byte[body.Length];
                int result;
                byte[] ivCopy = (byte[])iv.Clone();
                rc = BCryptDecrypt(hKey, body, body.Length, ref info, ivCopy, ivCopy.Length, outBuf, outBuf.Length, out result, 0);
                Marshal.FreeHGlobal(info.pbNonce);
                Marshal.FreeHGlobal(info.pbTag);
                if (rc != 0) throw new Exception("decrypt rc=" + rc);
                return outBuf;
            } finally { BCryptDestroyKey(hKey); }
        } finally { BCryptCloseAlgorithmProvider(hAlg, 0); }
    }
}
"@
} catch {
    $ideReady = $false
    Write-Host "    IDE 写入组件加载失败，跳过 CodeBuddy IDE 部分（$($_.Exception.Message)）" -ForegroundColor Red
}

Add-Type -AssemblyName System.Security
if ($ideReady) {
function Find-IdeExe($ideName) {
    if ($script:exePaths -and $script:exePaths[$ideName]) { return $script:exePaths[$ideName] }
    $exeName = "$ideName.exe"
    $cands = @()
    foreach ($r in @($env:LOCALAPPDATA, $pf, $pfx)) {
        $cands += (Join-Path $r "Programs\$ideName\$exeName")
        $cands += (Join-Path $r "$ideName\$exeName")
    }
    foreach ($d in @('D:', 'E:')) {
        $cands += "$d\$ideName\$exeName"
        $cands += "$d\Program Files\$ideName\$exeName"
        $cands += "$d\Apps\$ideName\$exeName"
        $cands += "$d\WorkSpace\Apps\$ideName\$ideName\$exeName"
    }
    $cands += (Join-Path $env:LOCALAPPDATA "Programs\codebuddy\$exeName")
    $exe = $cands | Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($exe) { return $exe }
    foreach ($rp in @("HKCU:\Software\Microsoft\Windows\CurrentVersion\App Paths\$exeName",
                      "HKLM:\Software\Microsoft\Windows\CurrentVersion\App Paths\$exeName")) {
        try {
            $v = (Get-ItemProperty $rp -ErrorAction Stop).'(default)'
            if ($v -and (Test-Path $v)) { return $v }
        } catch {}
    }
    foreach ($root in @('HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*',
                        'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*',
                        'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*')) {
        $items = Get-ItemProperty $root -ErrorAction SilentlyContinue
        foreach ($it in $items) {
            if ($it.DisplayName -and $it.DisplayName -like "*$ideName*" -and $it.InstallLocation) {
                $p = Join-Path $it.InstallLocation $exeName
                if (Test-Path $p) { return $p }
            }
        }
    }
    try {
        $w = & where.exe $exeName 2>$null | Select-Object -First 1
        if ($w -and (Test-Path $w)) { return $w }
    } catch {}
    return $null
}
function Get-ProductStorageKey($exePath) {
    if (-not $exePath) { return $null }
    $pj = Join-Path (Split-Path $exePath -Parent) 'resources\app\extensions\genie\product.json'
    if (-not (Test-Path $pj)) { return $null }
    try {
        $data = Get-Content $pj -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json
        return $data.authentication.attributes.storageKey
    } catch { return $null }
}
function Decrypt-ValueToText($aesKey, $valueJson) {
    try {
        # 不走 ConvertFrom-Json：PS 5.1 对 byte[] 形状的处理有坑，直接从文本里抠字节数组
        $m = [regex]::Match($valueJson, '"data"\s*:\s*\[([0-9,]+)\]')
        if (-not $m.Success) { $m = [regex]::Match($valueJson, '"value"\s*:\s*\[([0-9,]+)\]') }
        if (-not $m.Success) { $script:lastDecryptError = '值格式不对（找不到 data 数组）'; return $null }
        $raw = [byte[]]($m.Groups[1].Value -split ',' | ForEach-Object { [int]$_ })
        if ($raw.Length -le 31) { $script:lastDecryptError = '数据太短'; return $null }
        $ivb = [byte[]]$raw[3..14]
        $rest = [byte[]]$raw[15..($raw.Length - 1)]
        return [System.Text.Encoding]::UTF8.GetString([Gcm]::Decrypt($aesKey, $ivb, $rest))
    } catch {
        $script:lastDecryptError = $_.Exception.Message
        return $null
    }
}
$sessionJson = $session | ConvertTo-Json -Depth 12 -Compress
$plainBytes = [System.Text.Encoding]::UTF8.GetBytes($sessionJson)
$ideCount = 0
foreach ($ide in $ideDirs) {
    $dir = $ide.Dir
    $name = $ide.Name
    try {
        $lsPath = @((Join-Path $dir 'Local State'),
                    (Join-Path (Split-Path $dir -Parent) 'Local State')) |
            Where-Object { Test-Path $_ } | Select-Object -First 1
        if (-not $lsPath) { throw "没找到 Local State 文件（这个数据目录不完整，可能只是残留目录）" }
        $ls = Get-Content $lsPath -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json
        $encKey = [Convert]::FromBase64String($ls.os_crypt.encrypted_key)
        if ($encKey.Length -lt 10 -or [System.Text.Encoding]::ASCII.GetString($encKey[0..4]) -ne 'DPAPI') {
            throw "Local State 里的加密密钥格式不对"
        }
        $keyBytes = $encKey[5..($encKey.Length - 1)]
        $aesKey = [System.Security.Cryptography.ProtectedData]::Unprotect($keyBytes, $null, [System.Security.Cryptography.DataProtectionScope]::CurrentUser)
        $dbPath = Join-Path $dir 'User\globalStorage\state.vscdb'
        if (-not (Test-Path $dbPath)) { throw "没找到 state.vscdb（先打开一次 $name 再重跑）" }
        Copy-Item $dbPath "$dbPath.bak-$(Get-Date -Format 'yyyyMMddHHmmss')" -Force
        $db = [IntPtr]::Zero
        $rc = [Sqlite]::sqlite3_open16($dbPath, [ref]$db)
        if ($rc -ne 0) { throw "打开 state.vscdb 失败 rc=$rc" }
        try {
            # 目标 key 以 IDE 自身 product.json 的 storageKey 为准（版本不同 key 名不同，
            # 写错 IDE 就读不到），再并上 DB 里已有的登录条目 key —— 与桌面版切号逻辑一致
            $targets = @()
            $exeFound = Find-IdeExe $name
            $sk = Get-ProductStorageKey $exeFound
            if ($sk) {
                $targets += 'secret://{"extensionId":"tencent-cloud.coding-copilot","key":"' + $sk + '"}'
                Write-Host "    $name 当前版本登录条目（product.json）：$sk"
            } else {
                Write-Host "    未读到 product.json 的 storageKey（exe：$(if ($exeFound) { $exeFound } else { '未定位' })），用已有条目/兜底 key" -ForegroundColor Yellow
            }
            $allSecret = [Sqlite]::Query1($db, "SELECT key FROM ItemTable WHERE key LIKE 'secret://%tencent-cloud.coding-copilot%'")
            $loginKeys = @($allSecret | Where-Object { $_ -match '"key"\s*:\s*"[^"]*(accessToken|refreshToken)[^"]*"' })
            foreach ($k in $loginKeys) { if ($targets -notcontains $k) { $targets += $k } }
            if (-not $targets.Count) {
                $targets = @('secret://{"extensionId":"tencent-cloud.coding-copilot","key":"planning-genie.new.accessTokencn"}',
                             'secret://{"extensionId":"tencent-cloud.coding-copilot","key":"planning-genie.new.accessToken"}')
            }
            # 对齐会话 id 到当前 IDE 正在用的存储标识（桌面版切号也会做这一步）
            if ($loginKeys.Count) {
                try {
                    $curVal = [Sqlite]::Query1($db, "SELECT value FROM ItemTable WHERE key = '$($loginKeys[0].Replace("'", "''"))'")
                    if ($curVal.Count) {
                        $curText = Decrypt-ValueToText $aesKey $curVal[0]
                        if ($curText) {
                            $curObj = $curText | ConvertFrom-Json
                            if ($curObj.id -and $curObj.id -ne $session.id) {
                                Write-Host "    会话 id 对齐当前 IDE：$($curObj.id)"
                                $session | Add-Member -NotePropertyName id -NotePropertyValue $curObj.id -Force
                                $sessionJson = $session | ConvertTo-Json -Depth 12 -Compress
                                $plainBytes = [System.Text.Encoding]::UTF8.GetBytes($sessionJson)
                            }
                        }
                    }
                } catch {}
            }
            # 先清掉旧登录条目再写入，避免 IDE 读到残留的旧条目
            foreach ($k in $loginKeys) {
                [Sqlite]::Exec($db, "DELETE FROM ItemTable WHERE key = ?", @($k))
            }
            foreach ($k in $targets) {
                $iv = New-Object byte[] 12
                $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
                $rng.GetBytes($iv)
                $ct = [Gcm]::Encrypt($aesKey, $iv, $plainBytes)
                $all = New-Object byte[] (3 + 12 + $ct.Length)
                [System.Text.Encoding]::ASCII.GetBytes('v10').CopyTo($all, 0)
                $iv.CopyTo($all, 3)
                $ct.CopyTo($all, 15)
                # 注意：PS 5.1 的 ConvertTo-Json 会把 byte[] 写成 {"value":[...],"Count":N}
                # 的对象形状，IDE 读不了 —— 必须手工拼这段 JSON
                $valJson = '{"data":[' + ($all -join ',') + '],"type":"Buffer"}'
                [Sqlite]::Exec($db, "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)", @($k, $valJson))
                $ideCount++
            }
            # 写后校验：把刚写的第一条读回来解密，确认里面是本账号（分阶段报卡点）
            $verified = $false
            $vstage = '未执行'
            try {
                $firstKey = $targets[0].Replace("'", "''")
                $back = [Sqlite]::Query1($db, "SELECT value FROM ItemTable WHERE key = '$firstKey'")
                if (-not $back.Count) {
                    $vstage = '读回失败：数据库里找不到刚写的条目'
                } else {
                    $decText = Decrypt-ValueToText $aesKey $back[0]
                    if (-not $decText) {
                        $vstage = "解密失败：$script:lastDecryptError"
                    } elseif ($decText -like "*$($session.account.uid)*") {
                        $verified = $true
                        $vstage = '通过'
                    } else {
                        $vstage = '解密成功但内容不是本账号'
                    }
                }
            } catch { $vstage = "校验异常：$($_.Exception.Message)" }
            if ($verified) {
                Write-Host "    写后校验通过（读回解密确认是本账号）" -ForegroundColor Green
            } else {
                Write-Host "    注意：写后校验未通过（$vstage），请把这张截图发我" -ForegroundColor Yellow
            }
        } finally { [void][Sqlite]::sqlite3_close($db) }
        $written += "CodeBuddy IDE（$name）"
        Write-Host "    CodeBuddy IDE（$name）登录态已写入" -ForegroundColor Green
    } catch {
        $skipped += "CodeBuddy IDE（$name：$($_.Exception.Message)）"
        Write-Host "    CodeBuddy IDE（$name）写入失败：$($_.Exception.Message)" -ForegroundColor Red
    }
}
if ($ideDirs.Count -eq 0 -and $ideInstalled) {
    $skipped += 'CodeBuddy IDE（已安装但未启动过，没有数据目录）'
    Write-Host "    CodeBuddy IDE 已安装但没找到数据目录，先打开一次再重跑" -ForegroundColor Yellow
}
}

Write-Host ""
if ($written.Count) {
    Write-Host "完成。已写入：$($written -join '；')" -ForegroundColor Green
    Write-Host "现在打开对应软件，应该已经是账号「$label」。"
} else {
    Write-Host "本次没有写入任何软件。跳过明细：$($skipped -join '；')" -ForegroundColor Yellow
}
if ($written.Count -and $skipped.Count) {
    Write-Host "跳过明细：$($skipped -join '；')" -ForegroundColor Yellow
}
