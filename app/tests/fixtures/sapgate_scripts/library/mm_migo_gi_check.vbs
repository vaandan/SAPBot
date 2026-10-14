' PURPOSE: MIGO_GI goods issue / return against a process order - loads items and runs CHECK only (never posts).
' PARAMS: AUFNR (required) order; BWART (optional, default 261; use 262 for return)
' RESULT: LOAD_MSG, ITEMS, CHECK_MSG
Dim F: F="wnd[0]/usr/ssubSUB_MAIN_CARRIER:SAPLMIGO:0003/subSUB_FIRSTLINE:SAPLMIGO:0011/"
Go "MIGO_GI"
s.FindById(F & "cmbGODYNPRO-ACTION").Key="A07": W
s.FindById(F & "cmbGODYNPRO-REFDOC").Key="R08": W
s.FindById(F & "ctxtGODEFAULT_TV-BWART").Text=Opt("BWART","261")
s.FindById(F & "subSUB_FIRSTLINE_REFDOC:SAPLMIGO:2070/ctxtGODYNPRO-ORDER_NUMBER").Text=Need("AUFNR")
s.FindById(F & "subSUB_FIRSTLINE_REFDOC:SAPLMIGO:2070/btnMIGO_OK_GO").Press: W
If Has("wnd[1]") Then s.FindById("wnd[1]").SendVKey 0: W
Res "LOAD_MSG", Sbar()
TableRows "wnd[0]/usr/ssubSUB_MAIN_CARRIER:SAPLMIGO:0003/subSUB_ITEMLIST:SAPLMIGO:0200/tblSAPLMIGOTV_GOITEM", 8
' Check button (no posting)
s.FindById("wnd[0]/tbar[1]/btn[7]").Press: W
Res "CHECK_MSG", Sbar()
Popups
