' PURPOSE: Identify the SAP GUI Logon Pad on this PC (saplogon.exe path + version, landscape files in use) and list
'          every connection it offers, from SAPUILandscape.xml (local, global/server, includes) and legacy saplogon.ini.
'          READ-ONLY: reads local files and the registry; enters no SAP transaction and changes nothing.
' PARAMS:  LANDSCAPE (optional) full path or URL of an extra landscape XML to read as well
' RESULT:  LOGONPAD, LOGONPAD_VERSION, OPEN_CONN (connections open right now), LANDSCAPE_FILE, MSGSERVER, ROUTER,
'          CONN = Name | SID | Type | Server/Group | SysNr | Message server | Router | Workspace folder | Source file,
'          CONN_COUNT
' Works under the sap-gate runner (uses Res/Opt) and standalone: cscript //nologo gui_logonpad_connections.vbs

Dim fso, sh, done, connCount, extra
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
Set done = CreateObject("Scripting.Dictionary")   ' landscape files already read
connCount = 0

Sub LpOut(k, v)
    On Error Resume Next
    Res k, v
    If Err.Number <> 0 Then
        Err.Clear
        WScript.Echo "RESULT|" & k & "|" & v
    End If
End Sub

Function LpReg(path)
    On Error Resume Next
    LpReg = ""
    LpReg = sh.RegRead(path)
    Err.Clear
End Function

Function LpEnv(t)
    LpEnv = sh.ExpandEnvironmentStrings(t)
End Function

' ---------- 1. Logon Pad executable ----------
Dim dirs, d, exe, found
found = ""
dirs = Array(LpReg("HKLM\SOFTWARE\WOW6432Node\SAP\SAP Shared\SAPsysdir"), LpReg("HKLM\SOFTWARE\SAP\SAP Shared\SAPsysdir"), _
             LpEnv("%ProgramFiles(x86)%\SAP\FrontEnd\SAPgui"), LpEnv("%ProgramFiles%\SAP\FrontEnd\SAPgui"))
For Each d In dirs
    If found = "" And d <> "" Then
        exe = fso.BuildPath(d, "saplogon.exe")
        If fso.FileExists(exe) Then found = exe
    End If
Next
If found = "" Then
    LpOut "LOGONPAD", "saplogon.exe not found in registry SAPsysdir or Program Files"
Else
    LpOut "LOGONPAD", found
    LpOut "LOGONPAD_VERSION", fso.GetFileVersion(found)
End If

' Connections open right now (only when run through the gate, which supplies session s)
On Error Resume Next
Dim app, c, i
Set app = Nothing
Set app = s.Parent.Parent
If Not app Is Nothing Then
    For i = 0 To app.Connections.Count - 1
        Set c = app.Connections.ElementAt(CLng(i))
        LpOut "OPEN_CONN", c.Description & " | sessions=" & c.Sessions.Count
    Next
End If
Err.Clear
On Error GoTo 0

' ---------- 2. Landscape XML files ----------
Function LpAttr(n, a)
    Dim v
    v = n.getAttribute(a)
    If IsNull(v) Then LpAttr = "" Else LpAttr = v
End Function

Function SysNr(server)
    ' "host:3215" -> 15 ; "host" (msg server group) -> ""
    Dim p
    SysNr = ""
    p = InStrRev(server, ":")
    If p > 0 Then
        p = Mid(server, p + 1)
        If Len(p) = 4 And Left(p, 2) = "32" Then SysNr = Right(p, 2)
    End If
End Function

Sub WalkNodes(parent, path, wsMap)
    Dim n, it, p
    For Each n In parent.selectNodes("Node")
        p = path & "/" & LpAttr(n, "name")
        For Each it In n.selectNodes("Item")
            wsMap(LpAttr(it, "serviceid")) = p
        Next
        WalkNodes n, p, wsMap
    Next
End Sub

Sub ReadLandscape(src)
    Dim key, x, ok, ms, rt, wsMap, n, w, svc, srv, msName, rtName, ws, inc
    If src = "" Then Exit Sub
    key = LCase(src)
    If done.Exists(key) Then Exit Sub
    done(key) = 1
    If InStr(src, "://") = 0 And Not fso.FileExists(src) Then Exit Sub

    Set x = CreateObject("MSXML2.DOMDocument.6.0")
    x.async = False
    x.setProperty "ProhibitDTD", False
    ok = x.load(src)
    If Not ok Then
        LpOut "LANDSCAPE_FILE", src & " (could not read: " & x.parseError.reason & ")"
        Exit Sub
    End If
    LpOut "LANDSCAPE_FILE", src

    Set ms = CreateObject("Scripting.Dictionary")
    For Each n In x.selectNodes("//Messageservers/Messageserver")
        ms(LpAttr(n, "uuid")) = LpAttr(n, "name") & " (" & LpAttr(n, "host") & LpIIf(LpAttr(n, "port") <> "", ":" & LpAttr(n, "port"), "") & ")"
        LpOut "MSGSERVER", LpAttr(n, "name") & " | host=" & LpAttr(n, "host") & " | port=" & LpAttr(n, "port") & " | " & LpAttr(n, "description")
    Next
    Set rt = CreateObject("Scripting.Dictionary")
    For Each n In x.selectNodes("//Routers/Router")
        rt(LpAttr(n, "uuid")) = LpAttr(n, "name") & " (" & LpAttr(n, "router") & ")"
        LpOut "ROUTER", LpAttr(n, "name") & " | " & LpAttr(n, "router")
    Next
    Set wsMap = CreateObject("Scripting.Dictionary")
    For Each w In x.selectNodes("//Workspaces/Workspace")
        WalkNodes w, LpAttr(w, "name"), wsMap
        For Each n In w.selectNodes("Item")
            wsMap(LpAttr(n, "serviceid")) = LpAttr(w, "name")
        Next
    Next

    For Each svc In x.selectNodes("//Services/Service")
        srv = LpAttr(svc, "server")
        msName = "": rtName = "": ws = ""
        If ms.Exists(LpAttr(svc, "msid")) Then msName = ms(LpAttr(svc, "msid"))
        If rt.Exists(LpAttr(svc, "routerid")) Then rtName = rt(LpAttr(svc, "routerid"))
        If wsMap.Exists(LpAttr(svc, "uuid")) Then ws = wsMap(LpAttr(svc, "uuid"))
        connCount = connCount + 1
        LpOut "CONN", LpAttr(svc, "name") & " | " & LpAttr(svc, "systemid") & " | " & LpAttr(svc, "type") & " | " & srv & " | " & _
            SysNr(srv) & " | " & msName & " | " & rtName & " | " & ws & " | " & fso.GetFileName(src)
    Next

    ' follow <Include url="..."/> (global landscape files referenced from the local one)
    For Each inc In x.selectNodes("//Includes/Include")
        ReadLandscape FileFromUrl(LpAttr(inc, "url"))
    Next
End Sub

Function FileFromUrl(u)
    If LCase(Left(u, 8)) = "file:///" Then
        FileFromUrl = Replace(Mid(u, 9), "/", "\")
    ElseIf LCase(Left(u, 7)) = "file://" Then
        FileFromUrl = "\\" & Replace(Mid(u, 8), "/", "\")
    Else
        FileFromUrl = u
    End If
    FileFromUrl = Replace(FileFromUrl, "%20", " ")
End Function

Function LpIIf(c, a, b)
    If c Then LpIIf = a Else LpIIf = b
End Function

Dim common, f
common = LpEnv("%APPDATA%\SAP\Common")
ReadLandscape LpEnv("%SAPLOGON_LSXML_FILE%")
ReadLandscape fso.BuildPath(common, "SAPUILandscape.xml")
For Each f In Array("HKCU\Software\SAP\SAPLogon\Options\LandscapeFileOnServer", _
                    "HKLM\SOFTWARE\SAP\SAPLogon\Options\LandscapeFileOnServer", _
                    "HKLM\SOFTWARE\WOW6432Node\SAP\SAPLogon\Options\LandscapeFileOnServer")
    ReadLandscape FileFromUrl(LpReg(f))
Next
ReadLandscape fso.BuildPath(common, "SAPUILandscapeGlobal.xml")

On Error Resume Next
extra = ""
extra = Opt("LANDSCAPE", "")
Err.Clear
On Error GoTo 0
ReadLandscape FileFromUrl(extra)

' ---------- 3. Legacy saplogon.ini (SAP GUI 7.40 and older) ----------
Sub ReadIni(p)
    Dim t, line, sect, k, v, eq, desc, srvr, db, msn, rtr, idx
    If Not fso.FileExists(p) Or done.Exists(LCase(p)) Then Exit Sub
    done(LCase(p)) = 1
    Set desc = CreateObject("Scripting.Dictionary"): Set srvr = CreateObject("Scripting.Dictionary")
    Set db = CreateObject("Scripting.Dictionary"): Set msn = CreateObject("Scripting.Dictionary")
    Set rtr = CreateObject("Scripting.Dictionary")
    Set t = fso.OpenTextFile(p, 1)
    Do While Not t.AtEndOfStream
        line = Trim(t.ReadLine)
        If Left(line, 1) = "[" Then
            sect = LCase(Mid(line, 2, Len(line) - 2))
        Else
            eq = InStr(line, "=")
            If eq > 5 And LCase(Left(line, 4)) = "item" Then
                k = Mid(line, 5, eq - 5): v = Mid(line, eq + 1)
                Select Case sect
                    Case "description": desc(k) = v
                    Case "server": srvr(k) = v
                    Case "database": db(k) = v
                    Case "mssysname": msn(k) = v
                    Case "router": rtr(k) = v
                End Select
            End If
        End If
    Loop
    t.Close
    If desc.Count > 0 Then LpOut "LANDSCAPE_FILE", p & " (legacy ini)"
    For Each idx In desc.Keys
        If Trim(desc(idx)) <> "" Then
            connCount = connCount + 1
            LpOut "CONN", desc(idx) & " | " & msn(idx) & " | SAPGUI | " & srvr(idx) & " | " & db(idx) & " |  | " & rtr(idx) & " |  | " & fso.GetFileName(p)
        End If
    Next
End Sub
ReadIni fso.BuildPath(common, "saplogon.ini")
ReadIni LpEnv("%WINDIR%\saplogon.ini")

LpOut "CONN_COUNT", connCount
