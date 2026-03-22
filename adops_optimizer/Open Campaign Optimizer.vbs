' Double-click this to open the app with no terminal window.
CreateObject("WScript.Shell").Run "cmd /c """ & CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName) & "\Open Campaign Optimizer.bat""", 0, False
