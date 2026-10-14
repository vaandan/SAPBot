' PURPOSE: Change the total quantity of a process order (COR2) and save - e.g. to test change documents. POSTS DATA.
' PARAMS: AUFNR (required) process order; QTY (required) new total quantity
' RESULT: QTY_BEFORE, STATUS_MSG
Go "COR2"
SetT "wnd[0]/usr/ctxtCAUFVD-AUFNR", Need("AUFNR")
Key 0
If Left(Sbar(),1)="E" Then Res "STATUS_MSG", Sbar(): WScript.Quit 1
Dim q: q="wnd[0]/usr/tabsTABSTRIP_5115/tabpKOZE/ssubSUBSCR_5115:SAPLCOKO:5120/txtCAUFVD-GAMNG"
Res "QTY_BEFORE", s.FindById(q).Text
s.FindById(q).Text=Need("QTY")
Key 0
If Has("wnd[1]") Then s.FindById("wnd[1]/tbar[0]/btn[0]").Press: W
s.FindById("wnd[0]/tbar[0]/btn[11]").Press: W
Popups
Res "STATUS_MSG", Sbar()
