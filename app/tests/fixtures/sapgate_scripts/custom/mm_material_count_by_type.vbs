' PURPOSE: Material count per material type. READ-ONLY (SE16N display + standard ALV export, never saves in SAP):
'          1) SE16N T134 -> ALV List > Export > Local File (text with tabs) -> P10_Table\<SYS>_T134_<ts>.txt
'          2) SE16N MARA (output fields MATNR, MTART, LVORM only) -> same export -> <SYS>_MARA_MTART_<ts>.txt
'          3) Counts MARA rows per MTART from the exported file and writes <SYS>_Material_Count_by_Type_<ts>.txt
' PARAMS:  none. OUT_SUB optional (default "P10_Table") = subfolder of the workspace output folder
' RESULT:  T134_FILE, MARA_FILE, MARA_ROWS, COUNT lines (type|description|total|flagged for deletion), SUMMARY_FILE
Dim fso, outDir, ts, sysId, g, r, typeTxt, cnt, del, f134, fMara, fSum

Set fso = CreateObject("Scripting.FileSystemObject")
Set typeTxt = CreateObject("Scripting.Dictionary")
Set cnt = CreateObject("Scripting.Dictionary")
Set del = CreateObject("Scripting.Dictionary")
outDir = OUT_DIR & "\" & Opt("OUT_SUB", "P10_Table")
sysId = s.Info.SystemName & s.Info.Client
ts = Year(Now) & Right("0" & Month(Now), 2) & Right("0" & Day(Now), 2) & "_" & Right("0" & Hour(Now), 2) & Right("0" & Minute(Now), 2)

' ---------- 1) T134 material types ----------
Go "SE16N"
SetT "wnd[0]/usr/ctxtGD-TAB", "T134"
Key 0
SetT "wnd[0]/usr/txtGD-MAX_LINES", "9999"
Key 8
Set g = Grid()
For r = 0 To g.RowCount - 1
    If r Mod 20 = 0 Then g.FirstVisibleRow = r: W
    typeTxt(Trim(g.GetCellValue(CLng(r), "MTART"))) = Trim(g.GetCellValue(CLng(r), "MTBEZ"))
Next
f134 = sysId & "_T134_" & ts & ".txt"
AlvExport g, f134
Res "T134_FILE", outDir & "\" & f134 & " exists=" & fso.FileExists(outDir & "\" & f134)

' ---------- 2) MARA material number + type ----------
Go "SE16N"
SetT "wnd[0]/usr/ctxtGD-TAB", "MARA"
Key 0
SetOutputs ",MATNR,MTART,LVORM,"
SetT "wnd[0]/usr/txtGD-MAX_LINES", ""
Key 8
Set g = Grid()
Res "MARA_ROWS", g.RowCount
fMara = sysId & "_MARA_MTART_" & ts & ".txt"
AlvExport g, fMara
Res "MARA_FILE", outDir & "\" & fMara & " exists=" & fso.FileExists(outDir & "\" & fMara)

' ---------- 3) count from the exported file ----------
CountFile outDir & "\" & fMara, g.GetDisplayedColumnTitle("MTART"), g.GetDisplayedColumnTitle("LVORM")
fSum = sysId & "_Material_Count_by_Type_" & ts & ".txt"
WriteSummary outDir & "\" & fSum
Res "SUMMARY_FILE", outDir & "\" & fSum
Res "SBAR", Sbar()

Function Grid()
    If Has("wnd[0]/usr/cntlRESULT_LIST/shellcont/shell") Then
        Set Grid = s.FindById("wnd[0]/usr/cntlRESULT_LIST/shellcont/shell")
    ElseIf Has("wnd[0]/shellcont/shell") Then
        Set Grid = s.FindById("wnd[0]/shellcont/shell")
    Else
        Res "NO_GRID", Sbar()
        Popups
        WScript.Quit 1
    End If
End Function

' Ticks the SE16N "Output" checkbox only for the wanted fields (scrolls the selection list)
Sub SetOutputs(want)
    Dim tcId, t, pos, maxPos, vr, i, nm
    tcId = "wnd[0]/usr/tblSAPLSE16NSELFIELDS_TC"
    Set t = s.FindById(tcId): maxPos = t.VerticalScrollbar.Maximum: pos = 0
    Do
        Set t = s.FindById(tcId): vr = t.VisibleRowCount
        For i = 0 To vr - 1
            nm = ""
            On Error Resume Next
            nm = UCase(Trim(t.GetCell(CLng(i), 6).Text))
            If nm <> "" Then t.GetCell(CLng(i), 5).Selected = (InStr(want, "," & nm & ",") > 0)
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

' Standard ALV export: List > Export > Local File > Text with Tabs, UTF-8, to the output folder
Sub AlvExport(grid, fileName)
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
    s.FindById("wnd[1]/usr/ctxtDY_FILENAME").Text = fileName
    s.FindById("wnd[1]/usr/ctxtDY_FILE_ENCODING").Text = "4110"
    s.FindById("wnd[1]/tbar[0]/btn[11]").Press
    W
    WScript.Sleep 1500
End Sub

Sub CountFile(path, typeLabel, delLabel)
    Dim ts2, ln, parts, k, iType, iDel, hdrFound, mt
    iType = -1: iDel = -1: hdrFound = False
    Set ts2 = fso.OpenTextFile(path, 1, False, 0)
    Do While Not ts2.AtEndOfStream
        ln = ts2.ReadLine
        parts = Split(ln, vbTab)
        If Not hdrFound Then
            For k = 0 To UBound(parts)
                If Trim(parts(k)) = typeLabel Then iType = k
                If Trim(parts(k)) = delLabel Then iDel = k
            Next
            If iType >= 0 Then hdrFound = True
        ElseIf UBound(parts) >= iType Then
            mt = Trim(parts(iType))
            If mt <> "" Then
                cnt(mt) = cnt(mt) + 1
                If iDel >= 0 Then
                    If UBound(parts) >= iDel Then
                        If Trim(parts(iDel)) <> "" Then del(mt) = del(mt) + 1
                    End If
                End If
            End If
        End If
    Loop
    ts2.Close
    If Not hdrFound Then Res "COUNT_ERROR", "header '" & typeLabel & "' not found in " & path
End Sub

Sub WriteSummary(path)
    Dim o, k, tot, totDel, keys
    Set o = fso.CreateTextFile(path, True, False)
    o.WriteLine "Material Type" & vbTab & "Description" & vbTab & "No. of Materials" & vbTab & "Flagged for Deletion (client)"
    For Each k In typeTxt.Keys
        o.WriteLine k & vbTab & typeTxt(k) & vbTab & (cnt(k) + 0) & vbTab & (del(k) + 0)
        Res "COUNT", k & "|" & typeTxt(k) & "|" & (cnt(k) + 0) & "|" & (del(k) + 0)
        tot = tot + cnt(k): totDel = totDel + del(k)
    Next
    For Each k In cnt.Keys
        If Not typeTxt.Exists(k) Then
            o.WriteLine k & vbTab & "(not in T134)" & vbTab & cnt(k) & vbTab & (del(k) + 0)
            Res "COUNT", k & "|(not in T134)|" & cnt(k) & "|" & (del(k) + 0)
            tot = tot + cnt(k): totDel = totDel + del(k)
        End If
    Next
    o.WriteLine "TOTAL" & vbTab & "" & vbTab & tot & vbTab & totDel
    o.Close
    Res "TOTAL", tot & "|" & totDel
End Sub
