from pathlib import Path

import pandas as pd

from sapbot.agent.batch import BatchSpec, run_batch

SEED = Path(__file__).resolve().parent / "fixtures" / "sapgate_scripts"


def test_script_batch_with_resume(ex, tmp_path):
    ex.library.import_folder(SEED)
    xl = tmp_path / "orders.xlsx"
    pd.DataFrame({"Order": ["1000001", "1000002", "1000003"]}).to_excel(xl, index=False)
    calls = []
    stop_after = {"n": 2}

    def stop():
        return len(calls) >= stop_after["n"]
    spec = BatchSpec(excel=str(xl), mode="script", script="library/pp_cor3_order_overview.vbs",
                     param_map={"AUFNR": "Order"})
    r1 = run_batch(ex, spec, on_row=lambda i, rec: calls.append(i), stop_flag=stop)
    assert r1["processed_now"] == 2 and not r1["complete"]
    r2 = run_batch(ex, spec, on_row=lambda i, rec: calls.append(i))
    assert r2["processed_now"] == 1 and r2["complete"]
    df = pd.read_excel(r2["results_file"], dtype=str)
    assert list(df["SAPBOT_STATUS"]) == ["OK", "OK", "OK"]
    assert "REL" in df["SAPBOT_RESULT"][0]
