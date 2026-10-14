' PURPOSE: Generic SE16N table export. READ-ONLY (SE16N display + standard ALV export, never saves in SAP):
'          SE16N <TABLE> -> optional single filter -> output only FIELDS -> F8 ->
'          ALV List > Export > Local File (text with tabs, UTF-8) -> <OUT_DIR>\<OUT_SUB>\<FILE>
' PARAMS:  TABLE (required); FIELDS (required) comma list of technical names to output; FIELDS2..FIELDS5 (optional)
'          continue the list (the gate limits parameter length); "-" in a field name stands for "_";
'          FILE (required) file name ("-" kept as is); OUT_SUB (optional, default "P10_Table");
'          FFIELD / FVALUE (optional) one selection field and its value (single value or LOW..HIGH)
' RESULT:  ROWS, COLS, FILE (with exists flag), SBAR
Dim fso, outDir, tbl, fileName, want, g, fval, lowV, highV, waitI

Set fso = CreateObject("Scripting.FileSystemObject")
outDir = OUT_DIR & "\" & Opt("OUT_SUB", "P10_Table")
tbl = UCase(Need("TABLE"))
' "-" in a field name stands for "_" (the gate does not allow "_" in parameters), e.g. MTPOS-MARA = MTPOS_MARA
want = Need("FIELDS") & "," & Opt("FIELDS2", "") & "," & Opt("FIELDS3", "") & "," & Opt("FIELDS4", "") & "," & Opt("FIELDS5", "")
want = "," & UCase(Replace(Replace(want, " ", ""), "-", "_")) & ","
fileName = Need("FILE")

InfoOk
Go "SE16N"
SetT "wnd[0]/usr/ctxtGD-TAB", tbl
Key 0
If Opt("FFIELD", "") <> "" Then
    fval = Opt("FVALUE", "")
    lowV = fval: highV = ""
    If InStr(fval, "..") > 0 Then lowV = Split(fval, "..")(0): highV = Split(fval, "..")(1)
    SetFilter Replace(UCase(Opt("FFIELD", "")), "-", "_"), lowV, highV
End If
SetOutputs want
SetT "wnd[0]/usr/txtGD-MAX_LINES", "99999999"
Key 8
' wait up to ~2 minutes for the result list; confirm the "Information" popup whenever it appears
For waitI = 1 To 400
    InfoOk
    If Has("wnd[0]/usr/cntlRESULT_LIST/shellcont/shell") Or Has("wnd[0]/shellcont/shell") Then Exit For
    WScript.Sleep 300
Next
W

If Has("wnd[0]/usr/cntlRESULT_LIST/shellcont/shell") Then
    Set g = s.FindById("wnd[0]/usr/cntlRESULT_LIST/shellcont/shell")
ElseIf Has("wnd[0]/shellcont/shell") Then
    Set g = s.FindById("wnd[0]/shellcont/shell")
Else
    Res "NO_GRID", Sbar()
    Popups
    WScript.Quit 1
End If
Res "ROWS", g.RowCount
Res "COLS", g.ColumnCount
AlvExport g, fileName
Res "FILE", outDir & "\" & fileName & " exists=" & fso.FileExists(outDir & "\" & fileName)
Res "SBAR", Sbar()

' Confirms an open "Information" popup (e.g. "Numerous rows can cause performance problems") with Enter
Sub InfoOk()
    If Has("wnd[1]") Then
        If s.FindById("wnd[1]").Text = "Information" Then
            Res "INFO", s.FindById("wnd[1]/usr/txtMESSTXT1").Text
            s.FindById("wnd[1]/tbar[0]/btn[0]").Press
            W
        End If
    End If
End Sub

' Ticks the SE16N "Output" checkbox only for the wanted fields (scrolls the selection list)
Sub SetOutputs(wantList)
    Dim tcId, t, pos, maxPos, vr, i, nm
    tcId = SelTc()
    Set t = s.FindById(tcId): maxPos = t.VerticalScrollbar.Maximum: pos = 0
    Do
        Set t = s.FindById(tcId): vr = t.VisibleRowCount
        For i = 0 To vr - 1
            nm = ""
            On Error Resume Next
            nm = UCase(Trim(t.GetCell(CLng(i), 6).Text))
            If nm <> "" Then t.GetCell(CLng(i), 5).Selected = (InStr(wantList, "," & nm & ",") > 0)
            Err.Clear
            On Error GoTo 0
        Next
        If pos >= maxPos Then Exit Do
        pos = pos + vr: If pos > maxPos Then pos = maxPos
        t.VerticalScrollbar.Position = pos
        W
    Loop
    Set t = s.FindById(tcId)
    If t.VerticalScrollbar.Position <> 0 Then t.VerticalScrollbar.Position = 0: W
End Sub

' Enters LOW (and HIGH) for one selection field (scrolls the selection list to find it)
Sub SetFilter(fld, lowV, highV)
    Dim tcId, t, pos, maxPos, vr, i, nm, found
    tcId = SelTc()
    Set t = s.FindById(tcId): maxPos = t.VerticalScrollbar.Maximum: pos = 0: found = False
    Do
        Set t = s.FindById(tcId): vr = t.VisibleRowCount
        For i = 0 To vr - 1
            nm = ""
            On Error Resume Next
            nm = UCase(Trim(t.GetCell(CLng(i), 6).Text))
            Err.Clear
            On Error GoTo 0
            If nm = fld Then
                t.GetCell(CLng(i), 2).Text = lowV
                If highV <> "" Then t.GetCell(CLng(i), 3).Text = highV
                found = True
                Exit For
            End If
        Next
        If found Or pos >= maxPos Then Exit Do
        pos = pos + vr: If pos > maxPos Then pos = maxPos
        t.VerticalScrollbar.Position = pos
        W
    Loop
    If Not found Then Res "FILTER_NOT_FOUND", fld: WScript.Quit 2
    Set t = s.FindById(tcId)
    If t.VerticalScrollbar.Position <> 0 Then t.VerticalScrollbar.Position = 0: W
End Sub

' Standard ALV export: List > Export > Local File > Text with Tabs, UTF-8, to the output folder
Sub AlvExport(grid, fName)
    grid.PressToolbarContextButton "&MB_EXPORT"
    grid.SelectContextMenuItem "&PC"
    W
    On Error Resume Next
    s.FindById("wnd[1]/usr/subSUBSCREEN_STEPLOOP:SAPLSPO5:0150/sub:SAPLSPO5:0150/radSPOPLI-SELFLAG[1,0]").Select
    Err.Clear
    On Error GoTo 0
    s.FindById("wnd[1]/tbar[0]/btn[0]").Press
    W
    s.FindById("wnd[1]/usr/ctxtDY_PATH").Text = outDir
    s.FindById("wnd[1]/usr/ctxtDY_FILENAME").Text = fName
    s.FindById("wnd[1]/usr/ctxtDY_FILE_ENCODING").Text = "4110"
    s.FindById("wnd[1]/tbar[0]/btn[11]").Press
    W
    WScript.Sleep 1500
End Sub

' Selection-criteria table control: directly on the screen (P10) or inside subscreen 0121 (S/4, e.g. SUP)
Function SelTc()
    SelTc = "wnd[0]/usr/tblSAPLSE16NSELFIELDS_TC"
    If Not Has(SelTc) Then SelTc = "wnd[0]/usr/subTAB_SUB:SAPLSE16N:0121/tblSAPLSE16NSELFIELDS_TC"
End Function
