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

$triggerDefinitions = [ordered]@{
    'daily.opportunity.0900' = @{
        Title = 'A股 09:00 盘前机会发现'
        DestinationKey = 'daily_open_close'
        PromptPath = 'automations/prompts/triggers/daily-opportunity-0900.md'
        ScheduledTime = '09:00:00'
        RRule = 'FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=0'
    }
    'daily.execution.0945' = @{
        Title = 'A股 09:45 异常发现'
        DestinationKey = 'daily_intraday'
        PromptPath = 'automations/prompts/triggers/daily-execution-0945.md'
        ScheduledTime = '09:45:00'
        RRule = 'FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=45'
    }
    'daily.execution.1030' = @{
        Title = 'A股 10:30 趋势确认'
        DestinationKey = 'daily_intraday'
        PromptPath = 'automations/prompts/triggers/daily-execution-1030.md'
        ScheduledTime = '10:30:00'
        RRule = 'FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=10;BYMINUTE=30'
    }
    'daily.execution.1430' = @{
        Title = 'A股 14:30 操作决策'
        DestinationKey = 'daily_intraday'
        PromptPath = 'automations/prompts/triggers/daily-execution-1430.md'
        ScheduledTime = '14:30:00'
        RRule = 'FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=14;BYMINUTE=30'
    }
    'daily.review.1520' = @{
        Title = 'A股 15:20 收盘复盘'
        DestinationKey = 'daily_open_close'
        PromptPath = 'automations/prompts/triggers/daily-review-1520.md'
        ScheduledTime = '15:20:00'
        RRule = 'FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=15;BYMINUTE=20'
    }
    'periodic.monthly' = @{
        Title = 'A股月度复盘'
        DestinationKey = 'periodic_review'
        PromptPath = 'automations/prompts/triggers/periodic-monthly.md'
        ScheduledTime = '19:00:00'
        RRule = 'FREQ=MONTHLY;BYMONTHDAY=1;BYHOUR=19;BYMINUTE=0'
    }
    'periodic.quarterly' = @{
        Title = 'A股季度复盘'
        DestinationKey = 'periodic_review'
        PromptPath = 'automations/prompts/triggers/periodic-quarterly.md'
        ScheduledTime = '19:30:00'
        RRule = 'FREQ=YEARLY;BYMONTH=1,4,7,10;BYMONTHDAY=2;BYHOUR=19;BYMINUTE=30'
    }
    'periodic.annual' = @{
        Title = 'A股年度复盘'
        DestinationKey = 'periodic_review'
        PromptPath = 'automations/prompts/triggers/periodic-annual.md'
        ScheduledTime = '20:00:00'
        RRule = 'FREQ=YEARLY;BYMONTH=1;BYMONTHDAY=3;BYHOUR=20;BYMINUTE=0'
    }
}

$requiredFiles = @(
    'AGENTS.md',
    'README.md',
    'automations/14_SCHEDULE_REGISTRY.md',
    'automations/15_RESULT_DELIVERY.md',
    'automations/contracts/ai-decision-message-v1.schema.json',
    'automations/thread-map.local.json',
    'automations/prompts/trigger-handoff.md',
    'automations/prompts/run-registered-task.md',
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
    'data/state/11_STOCK_STATE.csv',
    'scripts/automation_results.py'
) + @($triggerDefinitions.Values | ForEach-Object { $_.PromptPath })

foreach ($relativePath in $requiredFiles) {
    Assert-Condition (Test-Path -LiteralPath (Join-Path $projectRoot $relativePath)) "缺少文件: $relativePath"
}

$registryPath = Join-Path $projectRoot 'automations/14_SCHEDULE_REGISTRY.md'
if (Test-Path -LiteralPath $registryPath) {
    $registry = Get-Content -LiteralPath $registryPath -Raw
    Assert-Condition ($registry -match 'Registry ID: `ScheduleRegistry-local-v1\.5`') 'Registry ID 不是v1.5本地身份'
    Assert-Condition ($registry -match '\| `local_outbox` \|') 'Registry 未启用local_outbox投递策略'
    Assert-Condition ($registry -match 'automations/15_RESULT_DELIVERY\.md') 'Registry 未引用统一结果投递规范'
    Assert-Condition ($registry -match 'automations/prompts/trigger-handoff\.md') 'Registry 未引用公共转交规则'
    Assert-Condition ($registry -match 'automations/prompts/run-registered-task\.md') 'Registry 未引用统一工作入口'

    $routeTableMatch = [regex]::Match($registry, '(?s)## 2\. 任务注册表\s+(.*?)\s+注册表中的`task_name`')
    Assert-Condition $routeTableMatch.Success '无法定位Registry业务路由表'
    $routeTable = if ($routeTableMatch.Success) { $routeTableMatch.Groups[1].Value } else { '' }

    foreach ($taskKey in $triggerDefinitions.Keys) {
        $count = ([regex]::Matches($routeTable, [regex]::Escape("``$taskKey``"))).Count
        Assert-Condition ($count -eq 1) "Registry中task_key数量异常: $taskKey ($count)"
    }
}

$triggerHandoffPath = Join-Path $projectRoot 'automations/prompts/trigger-handoff.md'
if (Test-Path -LiteralPath $triggerHandoffPath) {
    $triggerHandoff = Get-Content -LiteralPath $triggerHandoffPath -Raw
    Assert-Condition ($triggerHandoff -match 'D:\\WILL\\STOCK\\stock_advisor') '公共转交规则缺少固定本地根目录'
    Assert-Condition ($triggerHandoff -match 'automations/thread-map\.local\.json') '公共转交规则未引用本地task/thread映射'
    Assert-Condition ($triggerHandoff -match 'automations/prompts/run-registered-task\.md') '公共转交规则未引用统一工作入口'
    Assert-Condition ($triggerHandoff -match 'send_message_to_thread') '公共转交规则未要求跨task/thread发送消息'
    Assert-Condition ($triggerHandoff -match '延迟不足15分钟') '公共转交规则缺少启动延迟容差'
    Assert-Condition ($triggerHandoff -notmatch 'automations/14_SCHEDULE_REGISTRY|automations/15_RESULT_DELIVERY|automation_results\.py') '公共转交规则包含业务执行入口'
}

$runnerPath = Join-Path $projectRoot 'automations/prompts/run-registered-task.md'
if (Test-Path -LiteralPath $runnerPath) {
    $runner = Get-Content -LiteralPath $runnerPath -Raw
    Assert-Condition ($runner -match 'D:\\WILL\\STOCK\\stock_advisor') '统一工作入口缺少固定本地根目录'
    Assert-Condition ($runner -match 'automations/14_SCHEDULE_REGISTRY\.md') '统一工作入口未引用Registry'
    Assert-Condition ($runner -match 'automations/15_RESULT_DELIVERY\.md') '统一工作入口未引用结果投递规范'
    Assert-Condition ($runner -match 'ResultStore') '统一工作入口未接入ResultStore'
    Assert-Condition ($runner -match 'scheduled_for') '统一工作入口缺少计划时点输入'
}

foreach ($entry in $triggerDefinitions.GetEnumerator()) {
    $descriptorPath = Join-Path $projectRoot $entry.Value.PromptPath
    if (-not (Test-Path -LiteralPath $descriptorPath)) {
        continue
    }

    $descriptor = Get-Content -LiteralPath $descriptorPath -Raw
    $taskKeyCount = ([regex]::Matches($descriptor, [regex]::Escape("``$($entry.Key)``"))).Count
    Assert-Condition ($taskKeyCount -eq 1) "Trigger descriptor中task_key数量异常: $($entry.Key) ($taskKeyCount)"
    Assert-Condition ($descriptor.Contains("task_title: ``$($entry.Value.Title)``")) "Trigger descriptor标题异常: $($entry.Key)"
    Assert-Condition ($descriptor.Contains("destination_key: ``$($entry.Value.DestinationKey)``")) "Trigger descriptor汇总任务异常: $($entry.Key)"
    Assert-Condition ($descriptor.Contains("scheduled_time: ``$($entry.Value.ScheduledTime)``")) "Trigger descriptor计划时点异常: $($entry.Key)"
    Assert-Condition ($descriptor -match 'schedule_condition:') "Trigger descriptor缺少启动窗口: $($entry.Key)"
    Assert-Condition ($descriptor -match 'automations/prompts/trigger-handoff\.md') "Trigger descriptor未引用公共转交规则: $($entry.Key)"
    Assert-Condition ($descriptor -notmatch 'automations/14_SCHEDULE_REGISTRY|automations/15_RESULT_DELIVERY|automation_results\.py') "Trigger descriptor包含业务执行入口: $($entry.Key)"
}

$legacyDispatchers = @(
    'automations/prompts/daily-intraday.md',
    'automations/prompts/daily-open-close.md',
    'automations/prompts/periodic-review.md'
)
foreach ($legacyDispatcher in $legacyDispatchers) {
    Assert-Condition (-not (Test-Path -LiteralPath (Join-Path $projectRoot $legacyDispatcher))) "仍保留旧多时点dispatcher: $legacyDispatcher"
}

$threadMap = $null
$threadMapPath = Join-Path $projectRoot 'automations/thread-map.local.json'
if (Test-Path -LiteralPath $threadMapPath) {
    try {
        $threadMap = Get-Content -LiteralPath $threadMapPath -Raw | ConvertFrom-Json
    }
    catch {
        Assert-Condition $false "本地task/thread映射不是合法JSON: $($_.Exception.Message)"
    }
}

if ($null -ne $threadMap) {
    Assert-Condition ($threadMap.project_root -eq 'D:\WILL\STOCK\stock_advisor') '本地task/thread映射项目根目录异常'
    Assert-Condition (@($threadMap.work_threads.PSObject.Properties).Count -eq 3) '汇总task/thread数量不是3'
    Assert-Condition (@($threadMap.trigger_threads.PSObject.Properties).Count -eq 8) '投递task/thread数量不是8'
    foreach ($entry in $triggerDefinitions.GetEnumerator()) {
        $mappedProperty = $threadMap.trigger_threads.PSObject.Properties[$entry.Key]
        $mapped = if ($null -ne $mappedProperty) { $mappedProperty.Value } else { $null }
        Assert-Condition ($null -ne $mapped) "本地task/thread映射缺少: $($entry.Key)"
        if ($null -eq $mapped) {
            continue
        }
        Assert-Condition ($mapped.title -eq $entry.Value.Title) "投递task/thread标题异常: $($entry.Key)"
        Assert-Condition ($mapped.destination_key -eq $entry.Value.DestinationKey) "投递task/thread汇总映射异常: $($entry.Key)"
        Assert-Condition ($mapped.automation_id -match '^a(?:-[a-z0-9-]+)?$') "自动化ID异常: $($entry.Key)"
        Assert-Condition ($mapped.thread_id -match '^01[a-z0-9-]+$') "投递task/thread ID异常: $($entry.Key)"
        $destinationProperty = $threadMap.work_threads.PSObject.Properties[$mapped.destination_key]
        $destination = if ($null -ne $destinationProperty) { $destinationProperty.Value } else { $null }
        Assert-Condition ($null -ne $destination) "汇总task/thread映射缺少: $($mapped.destination_key)"
        if ($null -ne $destination) {
            Assert-Condition ($destination.thread_id -match '^01[a-z0-9-]+$') "汇总task/thread ID异常: $($mapped.destination_key)"
        }
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
    $installedProjectConfigs = @()
    if (Test-Path -LiteralPath $automationRoot) {
        $installedProjectConfigs = @(Get-ChildItem -LiteralPath $automationRoot -Directory | ForEach-Object {
            $configPath = Join-Path $_.FullName 'automation.toml'
            if (Test-Path -LiteralPath $configPath) {
                $raw = Get-Content -LiteralPath $configPath -Raw
                if ($raw -match 'stock_advisor') {
                    [pscustomobject]@{ Id = $_.Name; Path = $configPath; Raw = $raw }
                }
            }
        })
    }

    foreach ($entry in $triggerDefinitions.GetEnumerator()) {
        $matches = @($installedProjectConfigs | Where-Object { $_.Raw.Contains("name = `"$($entry.Value.Title)`"") })
        Assert-Condition ($matches.Count -eq 1) "已安装自动化数量异常: $($entry.Value.Title) ($($matches.Count))"
        if ($matches.Count -ne 1) {
            continue
        }

        $config = $matches[0].Raw
        $mappedAutomationId = if ($null -ne $threadMap) { $threadMap.trigger_threads.PSObject.Properties[$entry.Key].Value.automation_id } else { $null }
        if ($null -ne $mappedAutomationId) {
            Assert-Condition ($matches[0].Id -eq $mappedAutomationId) "自动化ID与本地映射不一致: $($entry.Value.Title)"
        }
        Assert-Condition ($config -match '(?m)^kind = "heartbeat"$') "自动化不是heartbeat: $($entry.Value.Title)"
        Assert-Condition ($config -match '(?m)^status = "ACTIVE"$') "自动化不是ACTIVE: $($entry.Value.Title)"
        Assert-Condition ($config.Contains($entry.Value.PromptPath)) "自动化未指向trigger descriptor: $($entry.Value.Title)"
        Assert-Condition ($config.Contains("rrule = `"$($entry.Value.RRule)`"")) "自动化调度异常: $($entry.Value.Title)"
        Assert-Condition ($config -match '(?m)^notification_policy = "failed_runs_only"$') "自动化成功运行未静默通知: $($entry.Value.Title)"

        if ($null -ne $threadMap) {
            $expectedThreadId = $threadMap.trigger_threads.PSObject.Properties[$entry.Key].Value.thread_id
            Assert-Condition ($config.Contains("target_thread_id = `"$expectedThreadId`"")) "自动化未绑定对应投递task/thread: $($entry.Value.Title)"
        }

        $promptMatch = [regex]::Match($config, '(?m)^prompt = "(.*)"$')
        Assert-Condition $promptMatch.Success "自动化缺少prompt: $($entry.Value.Title)"
        if ($promptMatch.Success) {
            $installedPrompt = $promptMatch.Groups[1].Value
            Assert-Condition ($installedPrompt.Length -le 280) "自动化prompt过厚: $($entry.Value.Title)"
            Assert-Condition ($installedPrompt -notmatch 'task_key|Registry ID|ResultStore|send_message_to_thread') "自动化prompt含分发或业务规则: $($entry.Value.Title)"
        }
    }

    $activeProjectConfigs = @($installedProjectConfigs | Where-Object { $_.Raw -match '(?m)^status = "ACTIVE"$' })
    Assert-Condition ($activeProjectConfigs.Count -eq 8) "活动项目自动化数量异常: $($activeProjectConfigs.Count)"
}

if ($failures.Count -gt 0) {
    $failures | ForEach-Object { Write-Error $_ -ErrorAction Continue }
    throw "自动化校验失败，共 $($failures.Count) 项。"
}

Write-Output "自动化校验通过。项目: $projectRoot"
