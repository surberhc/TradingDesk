# dashboard/archive — retired dashboard code

Nothing in this folder runs. It is kept for reference and git history only.

## app_8501.py + its launchers (retired 2026-08-24)

`app_8501.py` was `dashboard/app.py`, the original Trading Desk dashboard served on
**port 8501**. It is fully superseded by the ground-up rebuild at
`dashboard/desk/desk_app.py` on **port 8502**, which is now the single desk dashboard.

Feature-by-feature, 8502 covers everything 8501 did:

| 8501 tab | Where it lives now (:8502) |
| --- | --- |
| Health | Desk Pulse + Feeds & Connections + History & Event Log |
| Backtests | Strategy 0 page — same metrics and the same downloadable plotly reports |
| Accounts | Control Plane — the whole CRM roster, not the 3-account paper read |
| S8 | `page_s8.py`, an exact port that reads recorded ticks (no gateway contact) |

The one panel 8501 had alone, "S0 Performance vs Model", was already dead: it read a
`nav_history.csv` written by `account_monitor_run.py`, and that file exists nowhere on
the machine while the `AccountMonitorDaily` task is disabled.

`launch_dashboard.bat`, `launch_dashboard.ps1` and `run_dashboard.bat` were 8501's
launchers. The live equivalents are `dashboard/desk/run_desk_autostart.cmd` (scheduled
task `DeskDashboard_8502`, at logon) and `dashboard/desk/launch_desk_dashboard.bat`
(the desktop shortcut).

`app_8501.py`'s pure S8 helpers were tested by `dashboard/test_s8_panels.py`; that test
now targets `page_s8.py` and lives at `dashboard/desk/test_s8_panels.py`.

## gamma_tab.py, health_extras.py, s5_tab.py

Older whole-desk tabs (EDGAR, GEX, S5) removed when the dashboard was scoped to S0.
