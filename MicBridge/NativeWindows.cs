using System.Diagnostics;
using System.Runtime.InteropServices;

namespace MicBridge;

internal static class NativeWindows
{
    public delegate bool EnumWindowsProc(nint hwnd, nint lParam);

    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc callback, nint lParam);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(nint hwnd);
    [DllImport("user32.dll")] public static extern bool IsWindow(nint hwnd);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(nint hwnd, out uint processId);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(nint hwnd, out Rect rect);
    [DllImport("user32.dll")] public static extern bool IsHungAppWindow(nint hwnd);
    [DllImport("user32.dll")] public static extern bool IsIconic(nint hwnd);
    [DllImport("user32.dll")] private static extern bool ShowWindowAsync(nint hwnd, int command);
    [DllImport("user32.dll")] private static extern bool SetWindowPos(nint hwnd, nint insertAfter, int x, int y, int cx, int cy, uint flags);
    [DllImport("dwmapi.dll")] private static extern int DwmSetWindowAttribute(nint hwnd, int attribute, ref int value, int size);

    [StructLayout(LayoutKind.Sequential)]
    public struct Rect { public int Left, Top, Right, Bottom; }

    public static void EnableDarkTitleBar(nint hwnd)
    {
        if (!OperatingSystem.IsWindowsVersionAtLeast(10, 0, 17763)) return;
        int enabled = 1;
        if (DwmSetWindowAttribute(hwnd, 20, ref enabled, sizeof(int)) != 0)
            DwmSetWindowAttribute(hwnd, 19, ref enabled, sizeof(int));
    }

    public static bool RestoreDiscordBehindOtherWindows(nint hwnd)
    {
        if (!IsIconic(hwnd)) return false;

        // Electron stops exposing a usable accessibility tree while minimized.
        // Restore without activation, then keep Discord at the bottom of the
        // normal window stack so the workaround does not interrupt the user.
        const int showNoActivate = 4;
        const uint noSize = 0x0001;
        const uint noMove = 0x0002;
        const uint noActivate = 0x0010;
        const uint showWindow = 0x0040;
        ShowWindowAsync(hwnd, showNoActivate);
        SetWindowPos(hwnd, new nint(1), 0, 0, 0, 0, noSize | noMove | noActivate | showWindow);
        return true;
    }

    public static nint FindDiscordWindow()
    {
        var pids = new HashSet<uint>();
        foreach (var process in Process.GetProcessesByName("Discord"))
        {
            using (process) pids.Add((uint)process.Id);
        }
        nint best = 0;
        long bestArea = 0;
        EnumWindows((hwnd, _) =>
        {
            if (!IsWindowVisible(hwnd)) return true;
            GetWindowThreadProcessId(hwnd, out uint pid);
            if (!pids.Contains(pid) || !GetWindowRect(hwnd, out var r)) return true;
            long area = Math.Max(0, r.Right - r.Left) * (long)Math.Max(0, r.Bottom - r.Top);
            if (area > bestArea) { best = hwnd; bestArea = area; }
            return true;
        }, 0);
        return best;
    }
}
