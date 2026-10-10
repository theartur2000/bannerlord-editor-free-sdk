# List Win32 message boxes (#32770) and their controls; optionally press one button by its text with
# BM_CLICK (no focus change, no mouse/keyboard). -names limits to processes whose name matches.
#   dialogs.ps1                       list every dialog on the desktop
#   dialogs.ps1 -press No -names Bannerlord,Watchdog,CrashUploader
param([string]$press = "", [string[]]$names = @())
if (-not ("DlgTool" -as [type])) { Add-Type @"
using System; using System.Text; using System.Runtime.InteropServices; using System.Collections.Generic;
public class DlgTool {
  delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc f, IntPtr l);
  [DllImport("user32.dll")] static extern bool EnumChildWindows(IntPtr p, EnumProc f, IntPtr l);
  [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetClassName(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] static extern IntPtr SendMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
  public static List<string> Run(uint[] pids, string button) {
    var log = new List<string>();
    EnumWindows((h, l) => {
      uint pid; GetWindowThreadProcessId(h, out pid);
      if (pids.Length > 0 && Array.IndexOf(pids, pid) < 0) return true;
      var c = new StringBuilder(64); GetClassName(h, c, 64);
      if (c.ToString() != "#32770" || !IsWindowVisible(h)) return true;
      var tt = new StringBuilder(256); GetWindowText(h, tt, 256);
      log.Add("DIALOG pid=" + pid + " title=[" + tt + "]");
      EnumChildWindows(h, (ch, l2) => {
        var t = new StringBuilder(1024); GetWindowText(ch, t, 1024); var cc = new StringBuilder(64); GetClassName(ch, cc, 64);
        if (t.Length > 0) log.Add("  " + cc + ": " + t.ToString().Replace("\r", " ").Replace("\n", " "));
        if (button.Length > 0 && cc.ToString() == "Button" && t.ToString().Replace("&", "") == button) {
          SendMessage(ch, 0x00F5, IntPtr.Zero, IntPtr.Zero); log.Add("  PRESSED " + button); }
        return true; }, IntPtr.Zero);
      return true; }, IntPtr.Zero);
    return log;
  }
}
"@ }
$pids = @()
if ($names.Count -gt 0) { $pids = @(Get-Process -Name $names -ErrorAction SilentlyContinue | ForEach-Object { [uint32]$_.Id }) ; if ($pids.Count -eq 0) { "no such process"; exit 1 } }
[DlgTool]::Run([uint32[]]$pids, $press) | ForEach-Object {
    if ($_ -match "^DIALOG pid=(\d+)") { $_ + " proc=" + (Get-Process -Id $Matches[1] -ErrorAction SilentlyContinue).Name } else { $_ }
}
