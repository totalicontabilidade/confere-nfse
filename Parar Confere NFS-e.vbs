' Encerra o servidor do Confere NFS-e que roda em segundo plano.
Option Explicit
Dim shell, fso, wmi, processos, p, achou, sinal, pasta

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
pasta = fso.GetParentFolderName(WScript.ScriptFullName)

' O laco que mantem o servidor no ar reinicia qualquer queda em 5 segundos.
' Este aviso diz a ele que desta vez a parada foi de proposito.
Set sinal = fso.CreateTextFile(pasta & "\parar.sinal", True)
sinal.Close

Set wmi = GetObject("winmgmts:\\.\root\cimv2")
Set processos = wmi.ExecQuery("SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name = 'pythonw.exe' OR Name = 'python.exe'")

achou = 0
For Each p In processos
    If Not IsNull(p.CommandLine) Then
        If InStr(LCase(p.CommandLine), "servidor.py") > 0 Then
            p.Terminate()
            achou = achou + 1
        End If
    End If
Next

If achou > 0 Then
    MsgBox "Confere NFS-e encerrado.", 64, "Confere NFS-e"
Else
    If fso.FileExists(pasta & "\parar.sinal") Then fso.DeleteFile pasta & "\parar.sinal"
    MsgBox "O Confere NFS-e nao estava rodando.", 64, "Confere NFS-e"
End If
