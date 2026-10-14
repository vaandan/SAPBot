' PURPOSE: Confirm a process order phase (COR6N) - partial or final - incl. the custom Supervisor/Crew fields; saves and reads the goods-movement error log if posting fails. POSTS DATA.
' PARAMS: AUFNR (required); VORNR (required) phase e.g. 0020 / 0999; YIELD (required); FINAL (optional Y/N, default N);
'         SUP_ROW (optional, default 3 = first entry of the Supervisor F4 list); CREW (optional, default A); GM_CHECK (optional Y = show goods movements before save)
' RESULT: STEP messages, GM rows, STATUS_MSG, ERRLOG rows
Dim P0: P0="wnd[0]/usr/ssubSUB01:SAPLCORU_S:0010/"
Dim C0: C0=P0 & "subSLOT_DET7:SAPLCORU_S:0900/subSLCUST:SAPLXCOF:0900/"
Go "COR6N"
SetT P0 & "subSLOT_HDR:SAPLCORU_S:5115/ctxtAFRUD-AUFNR", Need("AUFNR")
SetT P0 & "subSLOT_HDR:SAPLCORU_S:5115/ctxtAFRUD-VORNR", Need("VORNR")
Key 0
Res "ENTER_MSG", Sbar()
If Has(C0 & "ctxtAFRU-ZZSUPER") Then
  s.FindById(C0 & "ctxtAFRU-ZZSUPER").SetFocus
  s.FindById("wnd[0]").SendVKey 4: W
  If Has("wnd[1]/usr/lbl[16," & Opt("SUP_ROW","3") & "]") Then
    s.FindById("wnd[1]/usr/lbl[16," & Opt("SUP_ROW","3") & "]").SetFocus
    s.FindById("wnd[1]").SendVKey 2: W
  End If
  SetT C0 & "ctxtAFRU-ZZCREW", Opt("CREW","A")
  Key 0
End If
If UCase(Opt("FINAL","N"))="Y" Then
  s.FindById(P0 & "subSLOT_CONF_TYPE:SAPLCORU_S:0105/cmbAFRUD-AUERU").Key="X"
Else
  s.FindById(P0 & "subSLOT_CONF_TYPE:SAPLCORU_S:0105/cmbAFRUD-AUERU").Key=" "
End If
s.FindById(P0 & "subSLOT_DET1:SAPLCORU_S:0215/txtAFRUD-LMNGA").Text=Need("YIELD")
Key 0
Res "QTY_MSG", Sbar()
If UCase(Opt("GM_CHECK","N"))="Y" And Left(Sbar(),1)<>"W" Then
  s.FindById("wnd[0]/tbar[1]/btn[18]").Press: W
  If Has("wnd[0]/usr/subTABLE:SAPLCOWB:0500/tblSAPLCOWBTCTRL_0500") Then TableRows "wnd[0]/usr/subTABLE:SAPLCOWB:0500/tblSAPLCOWBTCTRL_0500", 8
End If
s.FindById("wnd[0]/tbar[0]/btn[11]").Press: W
Dim i, m
For i=1 To 6
  m=Sbar(): Res "STEP" & i, m
  If InStr(m,"saved")>0 Or Left(m,1)="E" Or InStr(m,"Error in goods")>0 Then Exit For
  If Has("wnd[1]") Then s.FindById("wnd[1]/tbar[0]/btn[0]").Press: W Else Key 0
Next
Res "STATUS_MSG", Sbar()
If InStr(Sbar(),"Error in goods")>0 And Has("wnd[0]/usr/btnALL_DETAIL") Then
  s.FindById("wnd[0]/usr/btnALL_DETAIL").Press: W
  If Has("wnd[0]/usr/cntlGRID1/shellcont/shell/shellcont[1]/shell/shellcont[1]/shell") Then GridRows "wnd[0]/usr/cntlGRID1/shellcont/shell/shellcont[1]/shell/shellcont[1]/shell", 20
End If
