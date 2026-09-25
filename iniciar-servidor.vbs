' Sobe o servidor do Confere NFS-e em segundo plano, sem janela e sem abrir o navegador.
' E o que roda quando o Windows liga. Se ja estiver no ar, nao faz nada.
Option Explicit
Dim shell, fso, pasta, http, noAr

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
pasta = fso.GetParentFolderName(WScript.ScriptFullName)

' Atencao: com On Error Resume Next, um erro DENTRO da condicao de um If faz o VBScript
' entrar no Then. Por isso o status e lido numa linha propria e so conta se nada falhou.
Dim st
noAr = False
st = 0
On Error Resume Next
Set http = CreateObject("MSXML2.ServerXMLHTTP.6.0")
http.setTimeouts 2000, 2000, 3000, 3000
http.open "GET", "http://127.0.0.1:8131/api/ping", False
http.send
If Err.Number = 0 Then st = http.status
If Err.Number <> 0 Then st = 0
Err.Clear
On Error GoTo 0
If st = 200 Then noAr = True

If Not noAr Then
    ' 0 = sem janela; False = nao espera
    shell.Run "cmd /c """ & pasta & "\servidor-continuo.bat""", 0, False
End If
