' Confere NFS-e - Totali
' Abre o sistema sem janela preta: sobe o servidor em segundo plano e abre o navegador.
' Se o servidor ja estiver no ar, so abre a tela.

Option Explicit
Dim shell, fso, pasta, python, url, http, noAr, i

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
pasta = fso.GetParentFolderName(WScript.ScriptFullName)
url = "http://localhost:8131"

python = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python312\pythonw.exe"
If Not fso.FileExists(python) Then python = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python311\pythonw.exe"
If Not fso.FileExists(python) Then python = "pythonw.exe"

' Ja esta rodando?
noAr = False
On Error Resume Next
Set http = CreateObject("MSXML2.ServerXMLHTTP.6.0")
http.setTimeouts 2000, 2000, 3000, 3000
http.open "GET", url & "/api/ping", False
http.send
If Err.Number = 0 And http.status = 200 Then noAr = True
Err.Clear
On Error GoTo 0

If Not noAr Then
    ' 0 = sem janela nenhuma; False = nao espera terminar
    shell.CurrentDirectory = pasta
    ' python.exe dentro de um laco que reinicia se cair; o firewall ja libera esse programa
    shell.Run "cmd /c """ & pasta & "\servidor-continuo.bat""", 0, False

    ' espera o servidor responder (ate ~40s)
    For i = 1 To 40
        WScript.Sleep 1000
        On Error Resume Next
        Set http = CreateObject("MSXML2.ServerXMLHTTP.6.0")
        http.setTimeouts 1500, 1500, 2000, 2000
        http.open "GET", url & "/api/ping", False
        http.send
        If Err.Number = 0 And http.status = 200 Then
            Err.Clear
            On Error GoTo 0
            Exit For
        End If
        Err.Clear
        On Error GoTo 0
    Next
End If

shell.Run url, 1, False
