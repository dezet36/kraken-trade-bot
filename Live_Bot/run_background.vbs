Set oShell = CreateObject("WScript.Shell")
BotDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
oShell.CurrentDirectory = BotDir
oShell.Run "cmd /c python bot.py >> """ & BotDir & "\service_stdout.log"" 2>> """ & BotDir & "\service_stderr.log""", 0, False
