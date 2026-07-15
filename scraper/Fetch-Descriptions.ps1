<#
    Fetch-Descriptions.ps1
    ----------------------
    Adds a `description` column to the SIGGRAPH 2026 schedule CSV by reading each
    session's abstract from its sub-page on s2026.conference-schedule.org.

    WINDOWS-FRIENDLY: pure PowerShell, no Python, no installs. Runs on the built-in
    Windows PowerShell 5.1 or PowerShell 7.

    SAFE BY DESIGN (this is why it won't get the IP banned like the first attempt did):
      * ONE request at a time. Never concurrent. Concurrency is what triggered the ban.
      * A pause after every request (default 2.5s), plus a longer pause every 40 requests.
      * A CIRCUIT BREAKER: if several requests fail in a row (the signature of a firewall
        block), it STOPS immediately instead of hammering the site and digging in deeper.
      * Resumable: progress is saved to descriptions_cache.csv after every batch. If it
        stops (finished, blocked, or you close it), just run it again — it skips everything
        already fetched and continues where it left off.

    HOW TO RUN (from the folder that contains this script and the CSV):
        powershell -ExecutionPolicy Bypass -File .\Fetch-Descriptions.ps1

    TEST FIRST (do this once to confirm it works before the ~50-min full run):
        powershell -ExecutionPolicy Bypass -File .\Fetch-Descriptions.ps1 -Limit 5

    The -ExecutionPolicy Bypass applies only to this one run; it changes nothing on the
    machine and needs no admin rights.

    Output: siggraph2026_schedule_with_descriptions.csv (same rows/order + description).
#>

param(
    [string]$InFile     = "siggraph2026_schedule.csv",
    [string]$OutFile    = "siggraph2026_schedule_with_descriptions.csv",
    [string]$CacheFile  = "descriptions_cache.csv",
    [double]$DelaySec   = 2.5,   # pause after EACH request. Raise to 4-5 for extra caution.
    [int]   $BatchSize  = 40,    # after this many requests, take a longer breather
    [double]$BatchPause = 20,    # length of that longer breather, seconds
    [int]   $MaxConsecutiveFailures = 4,  # circuit breaker: stop after this many fails in a row
    [int]   $Limit      = 0      # 0 = all URLs; set small (e.g. 5) for a quick test
)

$ErrorActionPreference = "Stop"

# Force TLS 1.2 (Windows PowerShell 5.1 otherwise defaults to old TLS the site rejects).
try {
    [Net.ServicePointManager]::SecurityProtocol = `
        [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }

$UserAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
             "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# --- extraction ------------------------------------------------------------
function Clean-Html([string]$t) {
    if (-not $t) { return "" }
    $t = [regex]::Replace($t, '(?i)<br\s*/?>', "`n")
    $t = [regex]::Replace($t, '(?i)</p\s*>', "`n`n")
    $t = [regex]::Replace($t, '<[^>]+>', '')
    $t = [System.Net.WebUtility]::HtmlDecode($t)
    $t = [regex]::Replace($t, '[ \t]+', ' ')
    $t = [regex]::Replace($t, '[ \t]*\n[ \t]*', "`n")
    $t = [regex]::Replace($t, '\n{3,}', "`n`n")
    return $t.Trim()
}

function Get-Description([string]$html) {
    # p=15 presentation pages: <span class="abstract">...</span></div>
    $m = [regex]::Match($html, '<span class="abstract">(.*?)</span></div>', 'Singleline')
    if ($m.Success) { return (Clean-Html $m.Groups[1].Value) }
    # p=16 session pages: <div class="info-section session-description">...Description</span>TEXT</div>
    $m = [regex]::Match($html,
        '<div class="info-section session-description">.*?</span>(.*?)</div>', 'Singleline')
    if ($m.Success) { return (Clean-Html $m.Groups[1].Value) }
    return ""
}

function Is-RealUrl([string]$u) {
    if ([string]::IsNullOrWhiteSpace($u)) { return $false }
    if ($u.Trim().EndsWith("/null")) { return $false }
    return $true
}

# --- cache (resume support) ------------------------------------------------
$cache = @{}
if (Test-Path $CacheFile) {
    Import-Csv $CacheFile | ForEach-Object { $cache[$_.url] = $_.description }
    Write-Host ("Loaded {0} cached descriptions from {1}" -f $cache.Count, $CacheFile)
}
function Save-Cache {
    $cache.GetEnumerator() |
        ForEach-Object { [pscustomobject]@{ url = $_.Key; description = $_.Value } } |
        Export-Csv -Path $CacheFile -NoTypeInformation -Encoding UTF8
}

# --- load rows, collect unique real URLs -----------------------------------
if (-not (Test-Path $InFile)) { throw "Input CSV not found: $InFile" }
$rows = Import-Csv $InFile
if (-not ($rows | Get-Member -Name session_url)) {
    throw "Expected a 'session_url' column in $InFile"
}

$urls = New-Object System.Collections.Generic.List[string]
$seen = @{}
foreach ($r in $rows) {
    $u = "$($r.session_url)".Trim()
    if ((Is-RealUrl $u) -and -not $seen.ContainsKey($u)) {
        $seen[$u] = $true
        $urls.Add($u)
    }
}
$todo = @($urls | Where-Object { -not $cache.ContainsKey($_) })
if ($Limit -gt 0) { $todo = @($todo | Select-Object -First $Limit) }

Write-Host ("{0} rows | {1} unique fetchable URLs | {2} cached | {3} to fetch" -f `
    $rows.Count, $urls.Count, $cache.Count, $todo.Count)
Write-Host ("Pace: 1 request at a time, ~{0}s apart. Est. ~{1} min for this run.`n" -f `
    $DelaySec, [math]::Round(($todo.Count * ($DelaySec + 0.7) + `
    [math]::Floor($todo.Count / $BatchSize) * $BatchPause) / 60))

# --- fetch loop (sequential, with backoff + circuit breaker) ---------------
$done = 0; $errors = 0; $consecFail = 0; $blocked = $false
foreach ($url in $todo) {
    $ok = $false; $html = ""
    for ($attempt = 0; $attempt -lt 3 -and -not $ok; $attempt++) {
        try {
            $resp = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 25 `
                        -Headers @{ "User-Agent" = $UserAgent } -MaximumRedirection 5
            $html = $resp.Content
            $ok = $true
        } catch {
            Start-Sleep -Seconds (5 * ($attempt + 1))   # 5s, 10s backoff between attempts
        }
    }

    if ($ok) {
        $cache[$url] = (Get-Description $html)
        $consecFail = 0
    } else {
        $errors++; $consecFail++
        Write-Warning ("no response for {0}" -f $url)
        if ($consecFail -ge $MaxConsecutiveFailures) { $blocked = $true; break }
    }

    $done++
    if (($done % 20) -eq 0) {
        Write-Host ("  ...{0}/{1} fetched (errors: {2})" -f $done, $todo.Count, $errors)
        Save-Cache
    }
    Start-Sleep -Seconds $DelaySec
    if (($done % $BatchSize) -eq 0) {
        Write-Host ("  --- batch breather: pausing {0}s ---" -f $BatchPause)
        Start-Sleep -Seconds $BatchPause
    }
}
Save-Cache

if ($blocked) {
    Write-Host ""
    Write-Warning ("STOPPED: {0} requests failed in a row -- this looks like the site " +
        "throttling/blocking your IP. Nothing was lost: progress is saved in {1}." -f `
        $MaxConsecutiveFailures, $CacheFile)
    Write-Warning ("Wait a while (30-60+ min), then run the SAME command again to resume. " +
        "If it keeps stopping immediately, this IP is banned too -- try another network.")
}

# --- write output CSV (always, so partial progress is usable) --------------
foreach ($r in $rows) {
    $u = "$($r.session_url)".Trim()
    $desc = ""
    if ((Is-RealUrl $u) -and $cache.ContainsKey($u)) { $desc = $cache[$u] }
    $r | Add-Member -NotePropertyName description -NotePropertyValue $desc -Force
}
$rows | Export-Csv -Path $OutFile -NoTypeInformation -Encoding UTF8

$filled = @($rows | Where-Object { $_.description -ne "" }).Count
Write-Host ""
Write-Host ("Done -> {0}" -f $OutFile)
Write-Host ("{0} of {1} rows have a description ({2} blank)." -f `
    $filled, $rows.Count, ($rows.Count - $filled))
if (-not $blocked) {
    Write-Host "If some are unexpectedly blank, just run the same command again to fill gaps."
}
