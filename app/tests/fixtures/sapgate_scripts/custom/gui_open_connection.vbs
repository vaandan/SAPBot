' PURPOSE: Open a SAP GUI connection to an application server (opens the logon screen only; never enters
'          credentials, never logs on, changes no SAP data).
' PARAMS:  HOST (required) server host/IP, e.g. 10.28.254.81; SYSNR (optional, default 00) instance number
' RESULT:  ALREADY_OPEN or OPENED, CONN_COUNT
Dim lpApp, lpHost, lpNr, lpCs, lpC, lpI, lpNew, lpFound
lpHost = Need("HOST")
lpNr = Opt("SYSNR", "00")
lpCs = "/H/" & lpHost & "/S/32" & lpNr
lpFound = False
Set lpApp = s.Parent.Parent
For lpI = 0 To lpApp.Connections.Count - 1
    Set lpC = lpApp.Connections.ElementAt(CLng(lpI))
    If InStr(1, lpC.ConnectionString, "/H/" & lpHost & "/", 1) > 0 Then
        lpFound = True
        Res "ALREADY_OPEN", lpC.Description & " | " & lpC.ConnectionString & " | sessions=" & lpC.Sessions.Count
    End If
Next
If Not lpFound Then
    Set lpNew = lpApp.OpenConnectionByConnectionString(lpCs, True)
    Res "OPENED", lpNew.Description & " | " & lpNew.ConnectionString
End If
Res "CONN_COUNT", lpApp.Connections.Count
