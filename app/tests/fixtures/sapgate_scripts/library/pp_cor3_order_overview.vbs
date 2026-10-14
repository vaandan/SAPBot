' PURPOSE: Display a process order (COR3): header status, quantities and material list with batches. READ-ONLY.
' PARAMS: AUFNR (required) process order; ROWS (optional, default 15) material list rows to print
' RESULT: ORDER_TYPE, STATUS, material list rows
Go "COR3"
SetT "wnd[0]/usr/ctxtCAUFVD-AUFNR", Need("AUFNR")
Key 0
If Left(Sbar(),1)="E" Then Res "STATUS_MSG", Sbar(): WScript.Quit 1
If Has("wnd[0]/usr/txtCAUFVD-AUART") Then Res "ORDER_TYPE", s.FindById("wnd[0]/usr/txtCAUFVD-AUART").Text
If Has("wnd[0]/usr/txtCAUFVD-STTXT") Then Res "STATUS", s.FindById("wnd[0]/usr/txtCAUFVD-STTXT").Text
s.FindById("wnd[0]/tbar[1]/btn[7]").Press: W
If Has("wnd[0]/usr/tblSAPLCOMKTCTRL_5120") Then TableRows "wnd[0]/usr/tblSAPLCOMKTCTRL_5120", CInt(Opt("ROWS","15"))
