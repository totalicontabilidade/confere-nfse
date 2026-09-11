' Encerra o servidor do Confere NFS-e que roda em segundo plano.
Option Explicit
Dim shell, wmi, processos, p, achou, resposta

Set shell = CreateObject("WScript.Shell")
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
    MsgBox "O Confere NFS-e nao estava rodando.", 64, "Confere NFS-e"
End If
