param([ValidateSet('list','execute')][string]$Mode='list', [string]$ScriptPath, [string]$Nonce, [int]$BrowserProcessId=0)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$OutputEncoding=[Console]::OutputEncoding
try {
    $windows = @(Get-Process -Name chrome -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle -match 'pixiv' })
    if ($Mode -eq 'list') {
        $items = @($windows | ForEach-Object { @{process_id=$_.Id; pixiv_window=$true} })
        ConvertTo-Json -InputObject $items -Compress
        exit 0
    }
    if ($BrowserProcessId -ne 0) { $windows = @($windows | Where-Object Id -eq $BrowserProcessId) }
    if ($windows.Count -ne 1) { throw 'browser_selection_required' }
    if ($Nonce -notmatch '^[a-f0-9]{32}$') { throw 'invalid_nonce' }
    $browser = $windows[0]
    $script = [IO.File]::ReadAllText([IO.Path]::GetFullPath($ScriptPath))
    if ($script.Length -gt 50000 -or -not $script.Contains('https://www.pixiv.net') -or -not $script.Contains('JPGPNG_ORGANIZER:')) { throw 'invalid_bridge_script' }
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class OrganizerWindow {
 [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
 [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
 [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
 [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint current,uint target,bool attach);
 [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
 [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
 [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd,int command);
 [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd,out uint processId);
}
'@
    $root = [System.Windows.Automation.AutomationElement]::FromHandle($browser.MainWindowHandle)
    $promptName = [System.Windows.Automation.OrCondition]::new(
        [System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::NameProperty,'Console prompt'),
        [System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::NameProperty,'控制台提示'))
    $condition = [System.Windows.Automation.AndCondition]::new(
        [System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::Edit),$promptName)
    $prompt = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants,$condition)
    if ([OrganizerWindow]::IsIconic($browser.MainWindowHandle)) { [void][OrganizerWindow]::ShowWindow($browser.MainWindowHandle,9) }
    try { $root.SetFocus() } catch {}
    [void][OrganizerWindow]::SetForegroundWindow($browser.MainWindowHandle)
    Start-Sleep -Milliseconds 250
    [uint32]$owner=0
    [void][OrganizerWindow]::GetWindowThreadProcessId([OrganizerWindow]::GetForegroundWindow(),[ref]$owner)
    if ($owner -ne $browser.Id) {
        $foregroundThread=[OrganizerWindow]::GetWindowThreadProcessId([OrganizerWindow]::GetForegroundWindow(),[ref]$owner)
        $currentThread=[OrganizerWindow]::GetCurrentThreadId()
        $attached=$false
        try {
            if ($foregroundThread -ne $currentThread) { $attached=[OrganizerWindow]::AttachThreadInput($currentThread,$foregroundThread,$true) }
            [void][OrganizerWindow]::BringWindowToTop($browser.MainWindowHandle)
            [void][OrganizerWindow]::SetForegroundWindow($browser.MainWindowHandle)
        } finally { if ($attached) { [void][OrganizerWindow]::AttachThreadInput($currentThread,$foregroundThread,$false) } }
        Start-Sleep -Milliseconds 200
        [void][OrganizerWindow]::GetWindowThreadProcessId([OrganizerWindow]::GetForegroundWindow(),[ref]$owner)
        if ($owner -ne $browser.Id) { throw 'browser_focus_required' }
    }
    if ($null -eq $prompt) {
        [System.Windows.Forms.SendKeys]::SendWait('^+j')
        $until=[DateTime]::UtcNow.AddSeconds(6)
        do {
            Start-Sleep -Milliseconds 250
            $prompt=$root.FindFirst([System.Windows.Automation.TreeScope]::Descendants,$condition)
        } while ($null -eq $prompt -and [DateTime]::UtcNow -lt $until)
    }
    if ($null -eq $prompt) { throw 'console_prompt_unavailable' }
    ([System.Windows.Automation.ValuePattern]$prompt.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)).SetValue($script)
    $prompt.SetFocus()
    Start-Sleep -Milliseconds 200
    [void][OrganizerWindow]::GetWindowThreadProcessId([OrganizerWindow]::GetForegroundWindow(),[ref]$owner)
    $focused=[System.Windows.Automation.AutomationElement]::FocusedElement
    if ($owner -ne $browser.Id -or $focused.Current.Name -notin @('Console prompt','控制台提示')) {
        ([System.Windows.Automation.ValuePattern]$prompt.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)).SetValue('')
        throw 'browser_focus_required'
    }
    [System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
    $resultCondition=[System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::NameProperty,('JPGPNG_ORGANIZER:'+$Nonce))
    $until=[DateTime]::UtcNow.AddSeconds(35)
    do {
        Start-Sleep -Milliseconds 150
        $field=$root.FindFirst([System.Windows.Automation.TreeScope]::Descendants,$resultCondition)
        if ($null -eq $field) { continue }
        $raw=([System.Windows.Automation.ValuePattern]$field.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)).Current.Value
        if (-not $raw -or $raw.Length -gt 2100000) { continue }
        try { $result=$raw | ConvertFrom-Json } catch { continue }
        if ($result.nonce -eq $Nonce) { $result | ConvertTo-Json -Depth 30 -Compress; exit 0 }
    } while ([DateTime]::UtcNow -lt $until)
    throw 'browser_result_timeout'
} catch {
    if ($null -ne $prompt) { try { ([System.Windows.Automation.ValuePattern]$prompt.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)).SetValue('') } catch {} }
    $known=@('browser_selection_required','invalid_nonce','invalid_bridge_script','browser_focus_required','console_prompt_unavailable','browser_result_timeout')
    $code=if ($_.Exception.Message -in $known) { $_.Exception.Message } else { 'browser_bridge_failed' }
    @{ok=$false;code=$code;nonce=$Nonce;trace=@(); bridge_line=$_.InvocationInfo.ScriptLineNumber; exception_type=$_.Exception.GetType().Name} | ConvertTo-Json -Compress
    exit 1
}
