import sys


def _service_name_from_args(default="TelegramCICDBot"):
    if "--service-name" in sys.argv:
        i = sys.argv.index("--service-name")
        if i + 1 < len(sys.argv):
            name = sys.argv[i + 1]
            del sys.argv[i:i + 2]
            return name
    return default

SERVICE_NAME = _service_name_from_args()

try:
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil
except Exception:  # pragma: no cover - Windows only
    print("pywin32 is required for Windows service mode. Install requirements.txt first.")
    raise


class BotWindowsService(win32serviceutil.ServiceFramework):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = SERVICE_NAME
    _svc_description_ = "Telegram CI/CD Bot Service"

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.stop_event)

    def SvcDoRun(self):
        servicemanager.LogInfoMsg(f"{SERVICE_NAME} starting")
        from bot.main import main
        main()


if __name__ == '__main__':
    win32serviceutil.HandleCommandLine(BotWindowsService)
