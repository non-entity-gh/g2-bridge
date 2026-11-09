"""List monitors with their resolution and refresh rate for the app's display picker.

On Windows the order follows EnumDisplayMonitors, which is also how FFmpeg's
gfxcapture numbers monitor_idx. Elsewhere the list is generic.
"""
from dataclasses import dataclass
import sys


@dataclass(frozen=True)
class Display:
    index: int
    width: int = 0
    height: int = 0
    refresh_hz: int = 0
    primary: bool = False

    def label(self):
        text = f'Display {self.index + 1}'
        if self.width:
            text += f': {self.width}x{self.height}'
        if self.refresh_hz > 1:
            text += f' at {self.refresh_hz} Hz'
        return text + (' (main)' if self.primary else '')


def list_displays():
    if sys.platform == 'win32':
        try:
            found = _windows_displays()
            if found:
                return found
        except OSError:
            pass
    return [Display(i, primary=i == 0) for i in range(4 if sys.platform == 'darwin' else 1)]


def _windows_displays():
    import ctypes
    from ctypes import wintypes

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.DWORD), ('rcMonitor', wintypes.RECT), ('rcWork', wintypes.RECT),
                    ('dwFlags', wintypes.DWORD), ('szDevice', wintypes.WCHAR * 32)]

    class DEVMODEW(ctypes.Structure):
        _fields_ = [('dmDeviceName', wintypes.WCHAR * 32), ('dmSpecVersion', wintypes.WORD),
                    ('dmDriverVersion', wintypes.WORD), ('dmSize', wintypes.WORD),
                    ('dmDriverExtra', wintypes.WORD), ('dmFields', wintypes.DWORD),
                    ('dmPositionX', wintypes.LONG), ('dmPositionY', wintypes.LONG),
                    ('dmDisplayOrientation', wintypes.DWORD), ('dmDisplayFixedOutput', wintypes.DWORD),
                    ('dmColor', ctypes.c_short), ('dmDuplex', ctypes.c_short),
                    ('dmYResolution', ctypes.c_short), ('dmTTOption', ctypes.c_short),
                    ('dmCollate', ctypes.c_short), ('dmFormName', wintypes.WCHAR * 32),
                    ('dmLogPixels', wintypes.WORD), ('dmBitsPerPel', wintypes.DWORD),
                    ('dmPelsWidth', wintypes.DWORD), ('dmPelsHeight', wintypes.DWORD),
                    ('dmDisplayFlags', wintypes.DWORD), ('dmDisplayFrequency', wintypes.DWORD),
                    ('dmICMMethod', wintypes.DWORD), ('dmICMIntent', wintypes.DWORD),
                    ('dmMediaType', wintypes.DWORD), ('dmDitherType', wintypes.DWORD),
                    ('dmReserved1', wintypes.DWORD), ('dmReserved2', wintypes.DWORD),
                    ('dmPanningWidth', wintypes.DWORD), ('dmPanningHeight', wintypes.DWORD)]

    user32 = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                       ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), callback_type, wintypes.LPARAM]
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW)]
    user32.EnumDisplaySettingsW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(DEVMODEW)]
    handles = []

    def collect(handle, _hdc, _rect, _data):
        handles.append(handle)
        return True

    if not user32.EnumDisplayMonitors(None, None, callback_type(collect), 0):
        raise ctypes.WinError(ctypes.get_last_error())
    displays = []
    for index, handle in enumerate(handles):
        info = MONITORINFOEXW(cbSize=ctypes.sizeof(MONITORINFOEXW))
        if not user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            displays.append(Display(index))
            continue
        mode = DEVMODEW(dmSize=ctypes.sizeof(DEVMODEW))
        # ENUM_CURRENT_SETTINGS reports the physical mode, unaffected by display scaling
        if user32.EnumDisplaySettingsW(info.szDevice, -1 & 0xFFFFFFFF, ctypes.byref(mode)):
            width, height, refresh = mode.dmPelsWidth, mode.dmPelsHeight, mode.dmDisplayFrequency
        else:
            width = info.rcMonitor.right - info.rcMonitor.left
            height = info.rcMonitor.bottom - info.rcMonitor.top
            refresh = 0
        displays.append(Display(index, width, height, refresh, bool(info.dwFlags & 1)))
    return displays
