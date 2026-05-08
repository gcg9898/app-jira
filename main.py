"""
JiraBoard - Executable entry point (PyInstaller).

Usage:
  JiraBoard.exe              -> launcher panel (default)
  JiraBoard.exe --mode=flask -> Flask server
  JiraBoard.exe --mode=tray  -> Tray app + hotkey
  JiraBoard.exe --mode=newtask -> New task popup only
"""
import sys
import multiprocessing


def run_flask():
    import app as flask_module
    flask_module.app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)


def run_tray():
    from tray_app import main as tray_main
    tray_main()


def run_launcher():
    from launcher import main as launcher_main
    launcher_main()


if __name__ == "__main__":
    multiprocessing.freeze_support()

    mode = "launcher"
    for arg in sys.argv[1:]:
        if arg.startswith("--mode="):
            mode = arg.split("=", 1)[1]
            break

    if mode == "flask":
        run_flask()
    elif mode == "tray":
        run_tray()
    elif mode == "newtask":
        from tray_app import TaskPopup
        popup = TaskPopup()
        popup.show()
    else:
        run_launcher()
