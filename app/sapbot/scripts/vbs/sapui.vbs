' SAPBot VBS helper library - injected by the SAPBot runner in front of every script it runs.
' Derived from the sap-gate library (C:\Claude_CoWork) so existing scripts run unchanged.
' Scripts must not call Attach or GetObject themselves: the runner attaches to the requested system/client.
' Available to scripts: s (GuiSession), P (parameters dictionary), OUT_DIR (workspace\output),
'   Go tcode | SetT id,value | Key n | W | Has(id) | Sbar() | Popups | Dump id,depth | TableRows id,n |
'   GridRows id,n | Buttons id | Need(name) | Opt(name,default) | Res key,value
Dim s
Sub Attach(sysName, client)
  Dim a, app, i, j, c
  Set a=GetObject("SAPGUI"): Set app=a.GetScriptingEngine
  For i=0 To app.Children.Count-1
    Set c=app.Children(CLng(i))
    For j=0 To c.Children.Count-1
      If UCase(c.Children(CLng(j)).Info.SystemName)=UCase(sysName) And CStr(c.Children(CLng(j)).Info.Client)=CStr(client) Then
        Set s=c.Children(CLng(j))
        WScript.Echo "ATTACHED|" & s.Info.SystemName & "|" & s.Info.Client & "|" & s.Info.User
        Exit Sub
      End If
    Next
  Next
  WScript.Echo "SESSION_NOT_FOUND|" & sysName & "|" & client: WScript.Quit 14
End Sub
' SAPBot: attach to one exact session (the bot's own session, ADR B3) by its id, e.g. /app/con[0]/ses[1].
Sub AttachId(sid)
  Dim a, app
  Set a=GetObject("SAPGUI"): Set app=a.GetScriptingEngine
  On Error Resume Next
  Set s=app.FindById(sid)
  If Err.Number<>0 Or TypeName(s)="Nothing" Or IsEmpty(s) Then WScript.Echo "SESSION_NOT_FOUND|" & sid: WScript.Quit 14
  On Error GoTo 0
  WScript.Echo "ATTACHED|" & s.Info.SystemName & "|" & s.Info.Client & "|" & s.Info.User
End Sub
Sub W()
  Do While s.Busy: WScript.Sleep 120: Loop
  WScript.Sleep 300
End Sub
Sub Go(tc)
  s.FindById("wnd[0]/tbar[0]/okcd").Text="/n" & tc: s.FindById("wnd[0]").SendVKey 0: W
End Sub
Function Has(id)
  Dim o: On Error Resume Next: Set o=s.FindById(id): Has=(Err.Number=0): Err.Clear: On Error GoTo 0
End Function
Sub SetT(id, v)
  s.FindById(id).Text=v
End Sub
Sub Key(n)
  s.FindById("wnd[0]").SendVKey n: W
End Sub
Function Sbar()
  Dim b: Set b=s.FindById("wnd[0]/sbar")
  Sbar=b.MessageType & ": " & b.Text
End Function
' Dumps the control tree of a window (ids, types, texts) so field ids can be found.
Sub Dump(wid, depthMax)
  DumpNode s.FindById(wid), 0, depthMax
End Sub
Sub DumpNode(o, d, dm)
  Dim k, t
  On Error Resume Next
  t=""
  t=o.Text
  WScript.Echo String(d*2," ") & o.Type & " | " & o.Id & " | " & Left(Replace(t,vbCrLf," "),60)
  If d<dm Then
    If o.ContainerType Then
      For k=0 To o.Children.Count-1
        DumpNode o.Children.ElementAt(k), d+1, dm
      Next
    End If
  End If
  On Error GoTo 0
End Sub
' Prints the first n rows of a table control as "title=value" pairs.
Sub TableRows(tid, n)
  Dim t, r, c, line, v
  Set t=s.FindById(tid)
  For r=0 To n-1
    line="ROW " & r & ": "
    For c=0 To t.Columns.Count-1
      v=""
      On Error Resume Next
      v=t.GetCell(r,c).Text
      If Err.Number=0 And Trim(v)<>"" Then line=line & t.Columns.ElementAt(c).Title & "=" & Trim(v) & " | "
      Err.Clear
      On Error GoTo 0
    Next
    WScript.Echo line
  Next
End Sub
' Prints the first n rows of an ALV grid (GuiGridView) as "column=value" pairs.
Sub GridRows(gid, n)
  Dim g, cols, r, c, line, mx
  Set g=s.FindById(gid): Set cols=g.ColumnOrder
  mx=g.RowCount-1: If n-1<mx Then mx=n-1
  WScript.Echo "GRID_ROWS|" & g.RowCount
  For r=0 To mx
    If r Mod 15=0 Then g.FirstVisibleRow=r
    line="ROW " & r & ": "
    For c=0 To cols.Count-1
      line=line & cols.ElementAt(c) & "=" & Trim(g.GetCellValue(r,cols.ElementAt(c))) & " | "
    Next
    WScript.Echo line
  Next
End Sub
Sub Buttons(cid)
  Dim o, k
  Set o=s.FindById(cid)
  For k=0 To o.Children.Count-1
    If o.Children.ElementAt(k).Type="GuiButton" Then WScript.Echo o.Children.ElementAt(k).Id & " | " & o.Children.ElementAt(k).Tooltip
  Next
End Sub
' Prints every popup window (wnd[1..3]) title.
Sub Popups()
  Dim k
  For k=1 To 3
    If Has("wnd[" & k & "]") Then WScript.Echo "POPUP wnd[" & k & "]: " & s.FindById("wnd[" & k & "]").Text
  Next
End Sub
' Required parameter passed by the runner (key=value), or stop with a clear message.
Function Need(k)
  If Not P.Exists(UCase(k)) Then WScript.Echo "MISSING_PARAM|" & UCase(k): WScript.Quit 3
  Need=P(UCase(k))
End Function
' Optional parameter with default.
Function Opt(k, dflt)
  If P.Exists(UCase(k)) Then Opt=P(UCase(k)) Else Opt=dflt
End Function
' Machine-readable result line for Cowork.
Sub Res(k, v)
  WScript.Echo "RESULT|" & k & "|" & v
End Sub
