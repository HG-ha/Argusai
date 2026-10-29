Option Explicit
' Hidden GUI launcher for Start Menu / desktop shortcuts.
' WshShell.Run window style 0 starts the console-subsystem exe without a visible CMD.
Dim sh, fso, here, exe
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
exe = here & "\ArgusAI.exe"
If Not fso.FileExists(exe) Then
  WScript.Quit 1
End If
sh.CurrentDirectory = here
sh.Run """" & exe & """ --gui", 0, False
