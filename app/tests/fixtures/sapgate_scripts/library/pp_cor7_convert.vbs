' PURPOSE: Convert one planned order to a process order (COR7) and save. POSTS DATA.
' PARAMS: PLNUM (required) planned order; AUART (optional) order type - blank = from production scheduling profile
' RESULT: ORDER, ORDER_TYPE, STATUS_MSG
Dim plnum: plnum=Need("PLNUM")
Go "COR7"
SetT "wnd[0]/usr/ctxtAFPOD-PLNUM", plnum
SetT "wnd[0]/usr/ctxtAUFPAR-PI_AUFART", Opt("AUART","")
Key 0
If Left(Sbar(),1)="E" Then Res "STATUS_MSG", Sbar(): WScript.Quit 1
Res "ORDER_TYPE", s.FindById("wnd[0]/usr/txtCAUFVD-AUART").Text
Res "SYSTEM_STATUS", s.FindById("wnd[0]/usr/txtCAUFVD-STTXT").Text
s.FindById("wnd[0]/tbar[0]/btn[11]").Press: W
Popups
Res "STATUS_MSG", Sbar()
Dim m: m=s.FindById("wnd[0]/sbar").Text
If InStr(m,"created")>0 Then Res "ORDER", Trim(Split(Split(m,"order ")(1)," ")(0))
