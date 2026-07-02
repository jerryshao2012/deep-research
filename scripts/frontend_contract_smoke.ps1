param(
    [string]$ApiUrl = "http://127.0.0.1:2024",
    [string]$ApiKey = "",
    [switch]$SkipStream
)

$ErrorActionPreference = "Stop"

function Invoke-Json {
    param(
        [string]$Method,
        [string]$Url,
        [object]$Body = $null
    )

    $headers = @{
        "Content-Type" = "application/json"
    }

    if ($ApiKey) {
        $headers["X-API-Key"] = $ApiKey
    }

    if ($null -ne $Body) {
        return Invoke-RestMethod -Method $Method -Uri $Url -Headers $headers -Body ($Body | ConvertTo-Json -Depth 20)
    }

    return Invoke-RestMethod -Method $Method -Uri $Url -Headers $headers
}

Write-Host "== Frontend contract smoke test =="
Write-Host "API: $ApiUrl"

$ok = Invoke-Json -Method GET -Url "$ApiUrl/ok"
if (-not $ok.ok) { throw "GET /ok failed contract" }
Write-Host "PASS /ok"

$health = Invoke-Json -Method GET -Url "$ApiUrl/health"
if (-not $health.status) { throw "GET /health missing status" }
Write-Host "PASS /health"

$thread = Invoke-Json -Method POST -Url "$ApiUrl/threads" -Body @{ metadata = @{ source = "smoke" } }
$threadId = $thread.thread_id
if (-not $threadId) { throw "POST /threads missing thread_id" }
Write-Host "PASS /threads -> $threadId"

$search = Invoke-Json -Method POST -Url "$ApiUrl/threads/search" -Body @{
    limit = 20
    offset = 0
    sort_by = "updated_at"
    sort_order = "desc"
}
if ($search -isnot [array]) { throw "POST /threads/search expected array" }
Write-Host "PASS /threads/search"

$patched = Invoke-Json -Method PATCH -Url "$ApiUrl/threads/$threadId" -Body @{ metadata = @{ custom_title = "Smoke" } }
if (-not $patched.metadata.custom_title) { throw "PATCH /threads/{id} failed" }
Write-Host "PASS /threads/{thread_id} PATCH"

$state = Invoke-Json -Method POST -Url "$ApiUrl/threads/$threadId/state" -Body @{ values = @{ files = @{ "readme.md" = "hello" } } }
if (-not $state.checkpoint) { throw "POST /threads/{id}/state missing checkpoint" }
Write-Host "PASS /threads/{thread_id}/state"

$run = Invoke-Json -Method POST -Url "$ApiUrl/threads/$threadId/runs" -Body @{
    assistant_id = "researcher"
    input = @{ messages = @(@{ role = "user"; content = "hello" }) }
}
$runId = $run.run_id
if (-not $runId) { throw "POST /threads/{id}/runs missing run_id" }
Write-Host "PASS /threads/{thread_id}/runs -> $runId"

$runs = Invoke-Json -Method GET -Url "$ApiUrl/threads/$threadId/runs"
if ($runs -isnot [array]) { throw "GET /threads/{id}/runs expected array" }
Write-Host "PASS /threads/{thread_id}/runs GET"

$singleRun = Invoke-Json -Method GET -Url "$ApiUrl/threads/$threadId/runs/$runId"
if (-not $singleRun.status) { throw "GET /threads/{id}/runs/{run_id} missing status" }
Write-Host "PASS /threads/{thread_id}/runs/{run_id}"

$cancel = Invoke-Json -Method POST -Url "$ApiUrl/threads/$threadId/runs/$runId/cancel?wait=true"
if (-not $cancel.status) { throw "POST /cancel missing status" }
Write-Host "PASS /threads/{thread_id}/runs/{run_id}/cancel"

if (-not $SkipStream) {
    Write-Host "Checking stream endpoint payload..."
    $streamBody = @{ assistant_id = "researcher"; input = @{ messages = @(@{ role = "user"; content = "stream hello" }) } } | ConvertTo-Json -Depth 20
    $streamOutput = curl.exe -sN -X POST "$ApiUrl/threads/$threadId/runs/stream" -H "Content-Type: application/json" -H "X-API-Key: $ApiKey" --data $streamBody

    if ($streamOutput -notmatch "event: metadata") { throw "Stream missing metadata event" }
    if ($streamOutput -notmatch "event: updates") { throw "Stream missing updates event" }
    if ($streamOutput -notmatch "event: values") { throw "Stream missing values event" }
    if ($streamOutput -notmatch "event: end") { throw "Stream missing end event" }
    Write-Host "PASS /threads/{thread_id}/runs/stream"
}

$deleted = Invoke-Json -Method DELETE -Url "$ApiUrl/threads/$threadId"
Write-Host "PASS /threads/{thread_id} DELETE"

Write-Host "All smoke checks passed."