param(
    [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
$ProjectPath = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not $PythonPath) {
    $PythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($PythonCommand) {
        $PythonPath = $PythonCommand.Source
    } else {
        $BundledPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
        if (Test-Path -LiteralPath $BundledPython) {
            $PythonPath = $BundledPython
        }
    }
}

if (-not $PythonPath -or -not (Test-Path -LiteralPath $PythonPath)) {
    throw "Python 3 не найден. Укажите путь: .\register_windows_task.ps1 -PythonPath 'C:\путь\python.exe'"
}

& $PythonPath -c "import PIL; import telegram_bot, work_russia_automation"
if ($LASTEXITCODE -ne 0) {
    throw "Для этого Python нужен Pillow. Установите Pillow и повторите запуск."
}

if (-not (Test-Path -LiteralPath (Join-Path $ProjectPath ".env"))) {
    throw "Не найден .env с токеном Telegram. Создайте .env по образцу .env.example."
}

$TaskName = "FoodJobsTelegramBot"
$UserId = "$env:USERDOMAIN\$env:USERNAME"
$Action = New-ScheduledTaskAction `
    -Execute $PythonPath `
    -Argument ('-u "{0}\telegram_bot.py"' -f $ProjectPath) `
    -WorkingDirectory $ProjectPath
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $UserId
$Settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable
$Principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Local Telegram publisher for Moscow cook and confectioner vacancies" `
    -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName
Write-Host "Задача $TaskName создана и запущена. Публикация остаётся выключенной в config.json до проверки тестовой группы."
