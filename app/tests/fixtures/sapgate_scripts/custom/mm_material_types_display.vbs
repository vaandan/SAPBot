' PURPOSE: List all material types (table T134 with descriptions from T134T) via SE16N display. READ-ONLY:
'          opens SE16N, enters the table name, executes (F8) and reads the ALV result grid. Never saves.
' PARAMS:  none (MAXROWS optional, default 9999)
' RESULT:  COLS (technical|title list), ROW lines (cells separated by " | "), ROW_COUNT, SBAR
Dim g, r, c, cols, line, nm, n

Go "SE16N"
SetT "wnd[0]/usr/ctxtGD-TAB", "T134"
Key 0
SetT "wnd[0]/usr/txtGD-MAX_LINES", Opt("MAXROWS", "9999")
Key 8

If Not Has("wnd[0]/usr/cntlRESULT_LIST/shellcont/shell") Then
    Res "SBAR", Sbar()
    Popups
    Dump "wnd[0]/usr", 2
    WScript.Quit 1
End If

Set g = s.FindById("wnd[0]/usr/cntlRESULT_LIST/shellcont/shell")
cols = ""
For c = 0 To g.ColumnCount - 1
    nm = g.ColumnOrder.ElementAt(CLng(c))
    cols = cols & nm & "=" & g.GetDisplayedColumnTitle(nm) & " | "
Next
Res "COLS", cols

n = g.RowCount
For r = 0 To n - 1
    If r Mod 20 = 0 Then g.FirstVisibleRow = r: W
    line = ""
    For c = 0 To g.ColumnCount - 1
        line = line & Trim(g.GetCellValue(CLng(r), g.ColumnOrder.ElementAt(CLng(c)))) & " | "
    Next
    Res "ROW", line
Next
Res "ROW_COUNT", n
Res "SBAR", Sbar()
