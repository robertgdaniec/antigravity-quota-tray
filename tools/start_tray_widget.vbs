Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
widgetScript = scriptDir & "\tray_widget.py"

' Pobranie ewentualnych argumentów (lub domyślne --show przy uruchomieniu bez parametrów)
args = ""
If WScript.Arguments.Count = 0 Then
    args = " --show"
Else
    For Each arg In WScript.Arguments
        args = args & " """ & arg & """"
    Next
End If

' Uruchomienie bez czarnego okna konsoli (SW_HIDE)
WshShell.Run "pythonw """ & widgetScript & """" & args, 0, False
