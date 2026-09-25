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

' Responde o servidor? Atencao: com On Error Resume Next, um erro DENTRO da condicao
' de um If faz o VBScript entrar no Then - por isso o status e lido numa linha propria.
Function NoArAgora()
    Dim h, st
    st = 0
    On Error Resume Next
    Set h = CreateObject("MSXML2.ServerXMLHTTP.6.0")
    h.setTimeouts 1500, 1500, 3000, 3000
    h.open "GET", url & "/api/ping", False
    h.send
    If Err.Number = 0 Then st = h.status
    If Err.Number <> 0 Then st = 0
    Err.Clear
    On Error GoTo 0
    NoArAgora = (st = 200)
End Function

' Ja esta rodando?
noAr = NoArAgora()

If Not noAr Then
    ' 0 = sem janela nenhuma; False = nao espera terminar
    shell.CurrentDirectory = pasta
    ' python.exe dentro de um laco que reinicia se cair; o firewall ja libera esse programa
    shell.Run "cmd /c """ & pasta & "\servidor-continuo.bat""", 0, False

    ' espera o servidor responder (ate ~40s)
    For i = 1 To 40
        WScript.Sleep 1000
        If NoArAgora() Then Exit For
    Next
End If

shell.Run url, 1, False
