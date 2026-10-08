"""Windows notification-area controls for this helper's own console and worker."""
import ctypes as ct
from ctypes import wintypes as wt
import hashlib
import os
import threading

READY_PROPERTY = 'DingTrackerLogHelperReady'
INTERVAL_PROPERTY = 'DingTrackerLogHelperInterval'
WM_CLOSE = 0x10


def window_title(client):
    return 'DingTrackerLogHelper:' + hashlib.sha256(str(client.resolve()).casefold().encode()).hexdigest()[:24]


def api():
    if os.name != 'nt':
        raise OSError('Tray mode is available on Windows only')
    user = ct.WinDLL('user32', use_last_error=True)
    kernel = ct.WinDLL('kernel32', use_last_error=True)
    shell = ct.WinDLL('shell32', use_last_error=True)
    signatures = [
        (user, 'FindWindowW', wt.HWND, [wt.LPCWSTR, wt.LPCWSTR]),
        (user, 'GetPropW', wt.HANDLE, [wt.HWND, wt.LPCWSTR]),
        (user, 'SetPropW', wt.BOOL, [wt.HWND, wt.LPCWSTR, wt.HANDLE]),
        (user, 'PostMessageW', wt.BOOL, [wt.HWND, wt.UINT, ct.c_size_t, ct.c_ssize_t]),
        (user, 'ShowWindow', wt.BOOL, [wt.HWND, ct.c_int]),
        (user, 'SetForegroundWindow', wt.BOOL, [wt.HWND]),
        (user, 'DestroyWindow', wt.BOOL, [wt.HWND]),
        (user, 'DefWindowProcW', ct.c_ssize_t, [wt.HWND, wt.UINT, ct.c_size_t, ct.c_ssize_t]),
        (user, 'CreatePopupMenu', wt.HMENU, []),
        (user, 'AppendMenuW', wt.BOOL, [wt.HMENU, wt.UINT, ct.c_size_t, wt.LPCWSTR]),
        (user, 'DestroyMenu', wt.BOOL, [wt.HMENU]),
        (user, 'TrackPopupMenu', wt.UINT, [wt.HMENU, wt.UINT, ct.c_int, ct.c_int, ct.c_int, wt.HWND, ct.c_void_p]),
        (user, 'GetCursorPos', wt.BOOL, [ct.POINTER(wt.POINT)]),
        (user, 'LoadIconW', wt.HICON, [wt.HINSTANCE, wt.LPCWSTR]),
        (user, 'RegisterWindowMessageW', wt.UINT, [wt.LPCWSTR]),
        (user, 'PostQuitMessage', None, [ct.c_int]),
        (kernel, 'GetModuleHandleW', wt.HMODULE, [wt.LPCWSTR]),
        (kernel, 'GetConsoleWindow', wt.HWND, []),
        (kernel, 'CreateMutexW', wt.HANDLE, [ct.c_void_p, wt.BOOL, wt.LPCWSTR]),
        (kernel, 'CloseHandle', wt.BOOL, [wt.HANDLE]),
    ]
    for dll, name, result, arguments in signatures:
        function = getattr(dll, name)
        function.restype, function.argtypes = result, arguments
    return user, kernel, shell


def find_worker(client, ready=True):
    user, _, _ = api()
    window = user.FindWindowW(None, window_title(client))
    return window if window and (not ready or user.GetPropW(window, READY_PROPERTY)) else None


def stop_worker(client):
    window = find_worker(client)
    if not window:
        return False
    user, _, _ = api()
    if not user.PostMessageW(window, WM_CLOSE, 0, 0):
        raise ct.WinError(ct.get_last_error())
    return True


def worker_interval(client):
    window = find_worker(client)
    if not window:
        return None
    user, _, _ = api()
    return user.GetPropW(window, INTERVAL_PROPERTY)


class Tray:
    def __init__(self, client, control):
        self.client, self.control = client, control
        self.user, self.kernel, self.shell = api()
        self.window = None
        self.console = self.kernel.GetConsoleWindow()
        self.ready = threading.Event()
        self.error = None
        self.status = 'Waiting for saved game data'
        self.thread = threading.Thread(target=self._run, name='DingTrackerTray', daemon=True)

    def start(self):
        self.thread.start()
        if not self.ready.wait(10):
            raise OSError('Tray startup timed out')
        if self.error:
            raise self.error

    def close(self):
        if self.window:
            self.user.PostMessageW(self.window, WM_CLOSE, 0, 0)
        self.thread.join(timeout=5)

    def update(self, status):
        self.status = status
        if self.window:
            self.user.PostMessageW(self.window, 0x8002, 0, 0)

    def command(self, command):
        if command == 100 and self.console:
            self.user.ShowWindow(self.console, 9)
            self.user.SetForegroundWindow(self.console)
        elif command == 101 and self.console:
            self.user.ShowWindow(self.console, 0)
        elif command == 102:
            self.control.scan_now()
        elif command == 103:
            self.control.stop()
            self.user.DestroyWindow(self.window)
        elif 200 <= command <= 203:
            self.control.set_interval((30, 60, 120, 300)[command - 200])
            self.user.SetPropW(self.window, INTERVAL_PROPERTY, self.control.interval)
            self.update(f'Checks every {self.control.interval} seconds')

    def _menu(self):
        menu = self.user.CreatePopupMenu()
        if not menu:
            return
        try:
            self.user.AppendMenuW(menu, 2, 0, self.status[:100])
            self.user.AppendMenuW(menu, 0x800, 0, None)
            for command, label in ((100, 'Show console'), (101, 'Hide console'), (102, 'Scan now')):
                self.user.AppendMenuW(menu, 0, command, label)
            self.user.AppendMenuW(menu, 0x800, 0, None)
            for command, interval in enumerate((30, 60, 120, 300), 200):
                self.user.AppendMenuW(menu, 8 if interval == self.control.interval else 0,
                                      command, f'Check every {interval} seconds')
            self.user.AppendMenuW(menu, 0x800, 0, None)
            self.user.AppendMenuW(menu, 0, 103, 'Exit helper')
            point = wt.POINT()
            self.user.GetCursorPos(ct.byref(point))
            self.user.SetForegroundWindow(self.window)
            # Return the chosen command without also dispatching WM_COMMAND.
            command = self.user.TrackPopupMenu(menu, 0x182, point.x, point.y, 0, self.window, None)
            self.command(command)
            self.user.PostMessageW(self.window, 0, 0, 0)
        finally:
            self.user.DestroyMenu(menu)

    def _run(self):
        mutex, registered, added = None, False, False
        try:
            ct.set_last_error(0)
            mutex = self.kernel.CreateMutexW(None, False, 'Local\\' + window_title(self.client))
            if not mutex:
                raise ct.WinError(ct.get_last_error())
            if ct.get_last_error() == 183:
                raise OSError('The helper is already running for this client')
            callback_type = ct.WINFUNCTYPE(ct.c_ssize_t, wt.HWND, wt.UINT, ct.c_size_t, ct.c_ssize_t)

            class WindowClass(ct.Structure):
                _fields_ = [('style', wt.UINT), ('procedure', callback_type), ('classExtra', ct.c_int),
                            ('windowExtra', ct.c_int), ('instance', wt.HINSTANCE), ('icon', wt.HICON),
                            ('cursor', wt.HANDLE), ('background', wt.HBRUSH),
                            ('menu', wt.LPCWSTR), ('name', wt.LPCWSTR)]

            class Guid(ct.Structure):
                _fields_ = [('a', wt.DWORD), ('b', wt.WORD), ('c', wt.WORD), ('d', ct.c_ubyte * 8)]

            class IconData(ct.Structure):
                _fields_ = [('size', wt.DWORD), ('window', wt.HWND), ('id', wt.UINT), ('flags', wt.UINT),
                            ('message', wt.UINT), ('icon', wt.HICON), ('tip', wt.WCHAR * 128),
                            ('state', wt.DWORD), ('stateMask', wt.DWORD), ('info', wt.WCHAR * 256),
                            ('timeout', wt.UINT), ('infoTitle', wt.WCHAR * 64), ('infoFlags', wt.DWORD),
                            ('guid', Guid), ('balloonIcon', wt.HICON)]

            self.user.RegisterClassW.argtypes, self.user.RegisterClassW.restype = [ct.POINTER(WindowClass)], wt.WORD
            self.user.UnregisterClassW.argtypes = [wt.LPCWSTR, wt.HINSTANCE]
            self.user.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                ct.c_int, ct.c_int, ct.c_int, ct.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, ct.c_void_p]
            self.user.CreateWindowExW.restype = wt.HWND
            self.shell.Shell_NotifyIconW.argtypes, self.shell.Shell_NotifyIconW.restype = [wt.DWORD, ct.POINTER(IconData)], wt.BOOL
            self.user.GetMessageW.argtypes, self.user.GetMessageW.restype = [ct.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT], ct.c_int
            self.user.TranslateMessage.argtypes = [ct.POINTER(wt.MSG)]
            self.user.DispatchMessageW.argtypes, self.user.DispatchMessageW.restype = [ct.POINTER(wt.MSG)], ct.c_ssize_t
            taskbar_created = self.user.RegisterWindowMessageW('TaskbarCreated')

            @callback_type
            def procedure(window, message, wparam, lparam):
                # No exception may escape a ctypes callback into Windows.
                try:
                    if message == WM_CLOSE:
                        self.control.stop()
                        self.user.DestroyWindow(window)
                        return 0
                    if message == 2:
                        self.user.PostQuitMessage(0)
                        return 0
                    if message == 0x8001:
                        if lparam == 0x203:
                            self.command(100)
                        elif lparam == 0x205:
                            self._menu()
                        return 0
                    if message == 0x111:
                        self.command(wparam & 0xffff)
                        return 0
                    if message == 0x8002:
                        self.icon.tip = ('Ding Tracker: ' + self.status)[:127]
                        self.shell.Shell_NotifyIconW(1, ct.byref(self.icon))
                        return 0
                    if message == taskbar_created:
                        self.shell.Shell_NotifyIconW(0, ct.byref(self.icon))
                        return 0
                except Exception as error:
                    self.status = str(error)
                return self.user.DefWindowProcW(window, message, wparam, lparam)

            instance = self.kernel.GetModuleHandleW(None)
            class_name = window_title(self.client)
            window_class = WindowClass(name=class_name, instance=instance, procedure=procedure)
            if not self.user.RegisterClassW(ct.byref(window_class)):
                raise ct.WinError(ct.get_last_error())
            registered = True
            self.window = self.user.CreateWindowExW(0, class_name, class_name, 0, 0, 0, 0, 0, None, None, instance, None)
            if not self.window:
                raise ct.WinError(ct.get_last_error())
            self.icon = IconData(size=ct.sizeof(IconData), window=self.window, id=1, flags=7, message=0x8001,
                                 icon=self.user.LoadIconW(None, ct.cast(ct.c_void_p(32512), wt.LPCWSTR)),
                                 tip='Ding Tracker Log Helper - right-click for controls')
            if not self.shell.Shell_NotifyIconW(0, ct.byref(self.icon)):
                raise OSError('Windows could not add the tray icon')
            added = True
            if not self.user.SetPropW(self.window, INTERVAL_PROPERTY, self.control.interval):
                raise ct.WinError(ct.get_last_error())
            if not self.user.SetPropW(self.window, READY_PROPERTY, 1):
                raise ct.WinError(ct.get_last_error())
            self.ready.set()
            message = wt.MSG()
            while not self.control.stopped.is_set():
                result = self.user.GetMessageW(ct.byref(message), None, 0, 0)
                if result == 0:
                    break
                if result == -1:
                    raise ct.WinError(ct.get_last_error())
                self.user.TranslateMessage(ct.byref(message))
                self.user.DispatchMessageW(ct.byref(message))
        except Exception as error:
            self.error = error
            self.control.stop()
        finally:
            if added:
                self.shell.Shell_NotifyIconW(2, ct.byref(self.icon))
            if self.window:
                self.user.DestroyWindow(self.window)
                self.window = None
            if registered:
                self.user.UnregisterClassW(class_name, instance)
            if mutex:
                self.kernel.CloseHandle(mutex)
            self.ready.set()
