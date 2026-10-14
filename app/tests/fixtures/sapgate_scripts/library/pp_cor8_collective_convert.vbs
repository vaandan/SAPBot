' PURPOSE: Collective conversion of planned orders (COR8) for a plant/material; converts selected list rows and saves. POSTS DATA.
' PARAMS: WERKS (required) plant; MATNR (required) material; ROWS (optional, default "0") comma list of list rows to convert, 0-based; AUART (optional)
' RESULT: LIST rows shown, STATUS_MSG
Go "COR8"
SetT "wnd[0]/usr/ctxtCAUFVD-WERKS", Need("WERKS")
SetT "wnd[0]/usr/ctxtCAUFVD-MATNR", Need("MATNR")
SetT "wnd[0]/usr/ctxtAUFPAR-PI_AUFART", Opt("AUART","")
s.FindById("wnd[0]/tbar[1]/btn[8]").Press: W
If Not Has("wnd[0]/usr/tblSAPLCOUPTCTRL_5200") Then Res "STATUS_MSG", Sbar(): WScript.Quit 1
TableRows "wnd[0]/usr/tblSAPLCOUPTCTRL_5200", 10
Dim t, r: Set t=s.FindById("wnd[0]/usr/tblSAPLCOUPTCTRL_5200")
For Each r In Split(Opt("ROWS","0"), ",")
  t.GetAbsoluteRow(CLng(Trim(r))).Selected=True
Next
s.FindById("wnd[0]/usr/btnPUSH_CONVERT").Press: W
Res "CONVERT_MSG", Sbar()
s.FindById("wnd[0]/tbar[0]/btn[11]").Press: W
Popups
Res "STATUS_MSG", Sbar()
