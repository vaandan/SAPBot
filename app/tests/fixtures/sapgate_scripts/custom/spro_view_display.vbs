' PURPOSE: Open an SPRO customizing activity by its transaction (e.g. V/08, OVKK, V/06, V/07) and print the table
'          shown on its first screen, scrolling through all rows. DISPLAY ONLY: only confirms Information popups
'          with Enter (e.g. "Client 400 has status 'not modifiable'"); never presses Save, never changes a field.
' PARAMS:  TCODE (required) customizing transaction; ROWS (optional, default 500) max rows to print
' RESULT:  TITLE, POPUP texts, COLS (column titles), ROW lines (cells separated by " | "), ROW_COUNT, SBAR
Dim spTc, spMax, spI, spTbl, spId, spR, spC, spLine, spTotal, spVis, spPos, spCols, spOut

Function spFindTable(o)
    Dim k, ch, r
    spFindTable = ""
    If o.Type = "GuiTableControl" Then
        spFindTable = o.Id
        Exit Function
    End If
    If o.ContainerType Then
        For k = 0 To o.Children.Count - 1
            Set ch = o.Children.ElementAt(CLng(k))
            r = spFindTable(ch)
            If r <> "" Then
                spFindTable = r
                Exit Function
            End If
        Next
    End If
End Function

spTc = Need("TCODE")
spMax = CLng(Opt("ROWS", "500"))
Go spTc

' confirm up to 3 Information popups (display-only notices)
For spI = 1 To 3
    If Has("wnd[1]") Then
        Res "POPUP", s.FindById("wnd[1]").Text & ": " & Sbar()
        If s.FindById("wnd[1]").Text = "Information" Then
            s.FindById("wnd[1]/tbar[0]/btn[0]").Press
            W
        End If
    End If
Next

Res "TITLE", s.FindById("wnd[0]/titl").Text
spId = spFindTable(s.FindById("wnd[0]/usr"))
If spId = "" Then
    Res "NO_TABLE", "no table control on the first screen - dumping controls"
    Dump "wnd[0]/usr", 3
Else
    Set spTbl = s.FindById(spId)
    spCols = ""
    For spC = 0 To spTbl.Columns.Count - 1
        spCols = spCols & spTbl.Columns.ElementAt(CLng(spC)).Title & " | "
    Next
    Res "COLS", spCols
    spTotal = 0
    spPos = 0
    Do
        Set spTbl = s.FindById(spId)
        spVis = spTbl.VisibleRowCount
        For spR = 0 To spVis - 1
            If spPos + spR >= spTbl.RowCount Or spTotal >= spMax Then Exit For
            spLine = ""
            On Error Resume Next
            For spC = 0 To spTbl.Columns.Count - 1
                spOut = ""
                spOut = spTbl.GetCell(CLng(spR), CLng(spC)).Text
                If spTbl.GetCell(CLng(spR), CLng(spC)).Type = "GuiCheckBox" Then
                    If spTbl.GetCell(CLng(spR), CLng(spC)).Selected Then spOut = "X" Else spOut = ""
                End If
                spLine = spLine & Trim(spOut) & " | "
            Next
            On Error GoTo 0
            If Replace(Replace(spLine, "|", ""), " ", "") <> "" Then
                Res "ROW", spLine
                spTotal = spTotal + 1
            End If
        Next
        spPos = spPos + spVis
        If spPos >= spTbl.RowCount Or spTotal >= spMax Then Exit Do
        spTbl.VerticalScrollbar.Position = spPos
        W
    Loop
    Res "ROW_COUNT", spTotal
End If
Res "SBAR", Sbar()
