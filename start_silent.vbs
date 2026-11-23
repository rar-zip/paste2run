Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")
Dir = FSO.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = Dir
WshShell.Run "pythonw p2r.py serve", 0, False
