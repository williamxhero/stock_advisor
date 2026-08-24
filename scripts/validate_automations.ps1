[CmdletBinding()]
param(
    [switch]$CheckInstalled
)

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\')
$failures = [System.Collections.Generic.List[string]]::new()

function Assert-Condition {
    param(
        [bool]$Condition,
        [string]$Message
    )

    if (-not $Condition) {
        $script:failures.Add($Message)
    }
}

$requiredFiles = @(
    'AGENTS.md',
    'README.md',
    'automations/14_SCHEDULE_REGISTRY.md',
    'automations/prompts/daily-intraday.md',
    'automations/prompts/daily-open-close.md',
    'automations/prompts/periodic-review.md',
    'docs/governance/00_PROJECT_GUIDE.md',
    'docs/governance/13_DATA_SEMANTICS.md',
    'docs/strategy/02_TRADING_PLAYBOOK.md',
    'docs/research/03_CASEBOOK.md',
    'docs/research/04_HYPOTHESES.md',
    'docs/templates/06_DAILY_REVIEW_TEMPLATE.md',
    'docs/protocols/07_DAILY_EXECUTION_PROTOCOL.md',
    'docs/protocols/08_PERIODIC_REVIEW_PROTOCOL.md',
    'docs/protocols/09_OPPORTUNITY_DISCOVERY_PROTOCOL.md',
    'data/portfolio/01_CURRENT_PORTFOLIO.md',
    'data/logs/05_DECISION_LOG.csv',
    'data/logs/12_OPPORTUNITY_LOG.csv',
    'data/state/10_THEME_STATE.csv',
    'data/state/11_STOCK_STATE.csv'
)

foreach ($relativePath in $requiredFiles) {
    Assert-Condition (Test-Path -LiteralPath (Join-Path $projectRoot $relativePath)) "缺少文件: $relativePath"
}

$registryPath = Join-Path $projectRoot 'automations/14_SCHEDULE_REGISTRY.md'
if (Test-Path -LiteralPath $registryPath) {
    $registry = Get-Content -LiteralPath $registryPath -Raw
    Assert-Condition ($registry -match 'Registry ID: `ScheduleRegistry-local-') 'Registry ID 不是本地身份'

    $routeTableMatch = [regex]::Match($registry, '(?s)## 2\. 任务注册表\s+(.*?)\s+注册表中的`task_name`')
    Assert-Condition $routeTableMatch.Success '无法定位 Registry 业务路由表'
    $routeTable = if ($routeTableMatch.Success) { $routeTableMatch.Groups[1].Value } else { '' }

    $taskKeys = @(
        'daily.opportunity.0900',
        'daily.execution.0945',
        'daily.execution.1030',
        'daily.execution.1430',
        'daily.review.1520',
        'periodic.monthly',
        'periodic.quarterly',
        'periodic.annual'
    )

    foreach ($taskKey in $taskKeys) {
        $count = ([regex]::Matches($routeTable, [regex]::Escape("``$taskKey``"))).Count
        Assert-Condition ($count -eq 1) "Registry 中 task_key 数量异常: $taskKey ($count)"
    }
}

$dispatchers = [ordered]@{
    'automations/prompts/daily-intraday.md' = @('daily.execution.0945', 'daily.execution.1030', 'daily.execution.1430')
    'automations/prompts/daily-open-close.md' = @('daily.opportunity.0900', 'daily.review.1520')
    'automations/prompts/periodic-review.md' = @('periodic.monthly', 'periodic.quarterly', 'periodic.annual')
}

foreach ($entry in $dispatchers.GetEnumerator()) {
    $dispatcherPath = Join-Path $projectRoot $entry.Key
    if (-not (Test-Path -LiteralPath $dispatcherPath)) {
        continue
    }

    $dispatcher = Get-Content -LiteralPath $dispatcherPath -Raw
    Assert-Condition ($dispatcher -match 'D:\\WILL\\STOCK\\stock_advisor') "Dispatcher 缺少固定本地根目录: $($entry.Key)"
    Assert-Condition ($dispatcher -match 'automations/14_SCHEDULE_REGISTRY\.md') "Dispatcher 未引用 Registry: $($entry.Key)"
    foreach ($taskKey in $entry.Value) {
        $count = ([regex]::Matches($dispatcher, [regex]::Escape("``$taskKey``"))).Count
        Assert-Condition ($count -eq 1) "Dispatcher 中 task_key 数量异常: $taskKey ($count)"
    }
}

$legacyRootFiles = @(
    '00_PROJECT_GUIDE.md', '01_CURRENT_PORTFOLIO.md', '02_TRADING_PLAYBOOK.md',
    '03_CASEBOOK.md', '04_HYPOTHESES.md', '05_DECISION_LOG.csv',
    '06_DAILY_REVIEW_TEMPLATE.md', '07_DAILY_EXECUTION_PROTOCOL.md',
    '08_PERIODIC_REVIEW_PROTOCOL.md', '09_OPPORTUNITY_DISCOVERY_PROTOCOL.md',
    '10_THEME_STATE.csv', '11_STOCK_STATE.csv', '12_OPPORTUNITY_LOG.csv',
    '13_DATA_SEMANTICS.md', '14_SCHEDULE_REGISTRY.md'
)

foreach ($legacyFile in $legacyRootFiles) {
    Assert-Condition (-not (Test-Path -LiteralPath (Join-Path $projectRoot $legacyFile))) "根目录仍有旧文件: $legacyFile"
}

if ($CheckInstalled) {
    $automationRoot = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.codex\automations'
    $expectedAutomations = [ordered]@{
        'a-09-45' = @{
            Name = '每日盘中操作'
            PromptPath = 'automations/prompts/daily-intraday.md'
            RRule = 'FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9,10,14;BYMINUTE=30,45'
        }
        'a-09-00' = @{
            Name = '每日盘前盘后'
            PromptPath = 'automations/prompts/daily-open-close.md'
            RRule = 'FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9,15;BYMINUTE=0,20'
        }
        'a' = @{
            Name = '月季年复盘'
            PromptPath = 'automations/prompts/periodic-review.md'
            RRule = 'FREQ=MONTHLY;BYMONTHDAY=1,2,3;BYHOUR=19,20;BYMINUTE=0,30'
        }
    }

    foreach ($entry in $expectedAutomations.GetEnumerator()) {
        $configPath = Join-Path $automationRoot "$($entry.Key)\automation.toml"
        Assert-Condition (Test-Path -LiteralPath $configPath) "未安装自动化: $($entry.Key)"
        if (-not (Test-Path -LiteralPath $configPath)) {
            continue
        }

        $config = Get-Content -LiteralPath $configPath -Raw
        Assert-Condition ($config -match '(?m)^kind = "heartbeat"$') "自动化不是 heartbeat: $($entry.Key)"
        Assert-Condition ($config -match '(?m)^status = "ACTIVE"$') "自动化不是 ACTIVE: $($entry.Key)"
        Assert-Condition ($config.Contains($entry.Value.PromptPath)) "自动化未指向 dispatcher: $($entry.Key)"
        Assert-Condition ($config.Contains("name = `"$($entry.Value.Name)`"")) "自动化名称异常: $($entry.Key)"
        Assert-Condition ($config.Contains("rrule = `"$($entry.Value.RRule)`"")) "自动化调度异常: $($entry.Key)"
        Assert-Condition ($config -match '(?m)^target_thread_id = "[^"]+"$') "自动化未绑定本地 task/thread: $($entry.Key)"

        $promptMatch = [regex]::Match($config, '(?m)^prompt = "(.*)"$')
        Assert-Condition $promptMatch.Success "自动化缺少 prompt: $($entry.Key)"
        if ($promptMatch.Success) {
            $installedPrompt = $promptMatch.Groups[1].Value
            Assert-Condition ($installedPrompt.Length -le 260) "自动化 prompt 过厚: $($entry.Key)"
            Assert-Condition ($installedPrompt -notmatch 'task_key|09:45|10:30|14:30|15:20|19:00|19:30|20:00|Registry ID') "自动化 prompt 含分发或业务规则: $($entry.Key)"
        }
    }

    if (Test-Path -LiteralPath $automationRoot) {
        $activeProjectAutomationIds = Get-ChildItem -LiteralPath $automationRoot -Directory | ForEach-Object {
            $configPath = Join-Path $_.FullName 'automation.toml'
            if (Test-Path -LiteralPath $configPath) {
                $config = Get-Content -LiteralPath $configPath -Raw
                if ($config -match '(?m)^status = "ACTIVE"$' -and $config -match 'stock_advisor') {
                    $_.Name
                }
            }
        } | Sort-Object

        $expectedIds = @($expectedAutomations.Keys) | Sort-Object
        Assert-Condition (($activeProjectAutomationIds -join ',') -eq ($expectedIds -join ',')) "活动项目自动化集合异常: $($activeProjectAutomationIds -join ', ')"
    }
}

if ($failures.Count -gt 0) {
    $failures | ForEach-Object { Write-Error $_ -ErrorAction Continue }
    throw "自动化校验失败，共 $($failures.Count) 项。"
}

Write-Output "自动化校验通过。项目: $projectRoot"
