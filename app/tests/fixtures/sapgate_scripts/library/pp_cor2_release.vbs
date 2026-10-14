' PURPOSE: Process order: availability check, component batch determination, release and save (COR2). POSTS DATA.
' PARAMS: AUFNR (required) process order; ATP (optional Y/N, default Y); BATCHDET (optional Y/N, default Y)
' RESULT: ATP_MSG, BATCHES (material list after determination), STATUS, STATUS_MSG
Go "COR2"
SetT "wnd[0]/usr/ctxtCAUFVD-AUFNR", Need("AUFNR")
Key 0
If Left(Sbar(),1)="E" Then Res "STATUS_MSG", Sbar(): WScript.Quit 1
If UCase(Opt("ATP","Y"))="Y" Then
  s.FindById("wnd[0]/tbar[1]/btn[34]").Press: W
  Res "ATP_MSG", Sbar()
  If Has("wnd[1]") Then s.FindById("wnd[1]").SendVKey 0: W
End If
If UCase(Opt("BATCHDET","Y"))="Y" Then
  s.FindById("wnd[0]/tbar[1]/btn[7]").Press: W
  s.FindById("wnd[0]/usr/btnPUSH_AMAK").Press: W
  s.FindById("wnd[0]/usr/btnPUSH_CHPI").Press: W
  Res "BATCHDET_MSG", Sbar()
  TableRows "wnd[0]/usr/tblSAPLCOMKTCTRL_5120", 12
End If
s.FindById("wnd[0]/tbar[1]/btn[30]").Press: W
Res "RELEASE_MSG", Sbar()
Popups
s.FindById("wnd[0]/tbar[0]/btn[11]").Press: W
Popups
Res "STATUS_MSG", Sbar()
