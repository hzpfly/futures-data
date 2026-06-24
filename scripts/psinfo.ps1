<#
.SYNOPSIS
    psinfo.ps1 -- 显示所有终端相关进程的详细信息

.DESCRIPTION
    列出所有 cmd.exe / conhost.exe 进程，包括 PID、启动时间、父进程、命令行、内存。
    超过 24h 的僵尸进程以灰色标记。

.PARAMETER All
    同时列出 PowerShell / Windows Terminal 进程

.PARAMETER Tree
    以树形结构按父子关系显示

.EXAMPLE
    .\scripts\psinfo.ps1              基础视图 (cmd + conhost)
    .\scripts\psinfo.ps1 -All         包含 powershell / wt
    .\scripts\psinfo.ps1 -Tree        树形视图
#>

param(
    [switch]$All,
    [switch]$Tree
)

$targets = @("cmd", "conhost")
if ($All) { $targets += "powershell", "pwsh", "wt", "WindowsTerminal" }

$procs = Get-Process -Name $targets -ErrorAction SilentlyContinue |
    Sort-Object { try { $_.StartTime } catch { [DateTime]::MinValue } }

if ($procs.Count -eq 0) {
    Write-Host "No matching processes found." -ForegroundColor Yellow
    exit
}

if ($Tree) {
    function Show-Tree($procId, $indent = "") {
        $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if (-not $p) { return }
        $cmd = (Get-WmiObject Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue).CommandLine
        if (-not $cmd) { $cmd = "" }
        if ($cmd.Length -gt 80) { $cmd = $cmd.Substring(0, 77) + "..." }
        $age = if ($p.StartTime) { [math]::Round(((Get-Date) - $p.StartTime).TotalHours, 1) } else { "?" }
        Write-Host ("{0}{1,-8} {2,-12} {3,6}h {4}" -f $indent, $procId, $p.ProcessName, $age, $cmd)
        $children = Get-WmiObject Win32_Process -Filter "ParentProcessId=$procId" -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty ProcessId
        foreach ($c in $children) {
            Show-Tree $c "$indent  +-- "
        }
    }

    Write-Host ""
    Write-Host "=== Process Tree ===" -ForegroundColor Cyan
    Write-Host ("{0}{1,-8} {2,-12} {3,6} {4}" -f "", "PID", "Name", "Age(h)", "CommandLine") -ForegroundColor Gray
    Write-Host ("-" * 100)

    $allIds = $procs | ForEach-Object { $_.Id }
    foreach ($p in $procs) {
        $parent = (Get-WmiObject Win32_Process -Filter "ProcessId=$($p.Id)" -ErrorAction SilentlyContinue).ParentProcessId
        if ($parent -notin $allIds) {
            Show-Tree $p.Id
        }
    }
}
else {
    Write-Host ""
    Write-Host "=== Terminal-related Processes ===" -ForegroundColor Cyan
    Write-Host ("{0,-8} {1,-12} {2,-22} {3,5} {4,-6} {5,-8} {6} {7}" -f `
        "PID", "Name", "StartTime", "Age(h)", "Parent", "(Parent)", "CommandLine (...tail)", "Mem(MB)") -ForegroundColor Gray
    Write-Host ("-" * 130)

    foreach ($p in $procs) {
        $wmi = Get-WmiObject Win32_Process -Filter "ProcessId=$($p.Id)" -ErrorAction SilentlyContinue
        $cmd = $wmi.CommandLine
        if (-not $cmd) { $cmd = "" }
        if ($cmd.Length -gt 65) { $cmd = "..." + $cmd.Substring($cmd.Length - 62) }
        $parentId = $wmi.ParentProcessId
        $parentName = (Get-Process -Id $parentId -ErrorAction SilentlyContinue).ProcessName
        if (-not $parentName) { $parentName = "-" }
        $start = if ($p.StartTime) { $p.StartTime.ToString("yyyy-MM-dd HH:mm:ss") } else { "-" }
        $age = if ($p.StartTime) { [math]::Round(((Get-Date) - $p.StartTime).TotalHours, 1) } else { "?" }
        $mem = [math]::Round($p.WorkingSet64 / 1MB, 1)

        $color = "White"
        if ($age -is [double]) {
            if ($age -gt 24) { $color = "DarkGray" }
            elseif ($age -gt 2) { $color = "Gray" }
        }

        Write-Host ("{0,-8} {1,-12} {2,-22} {3,4}h  {4,-6} {5,-8} {6} {7,5}MB" -f `
            $p.Id, $p.ProcessName, $start, $age, $parentId, "($parentName)", $cmd, $mem) -ForegroundColor $color
    }
}

Write-Host ""
Write-Host "Total: $($procs.Count) processes" -ForegroundColor Cyan
