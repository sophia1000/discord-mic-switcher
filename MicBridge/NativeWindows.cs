using System.Diagnostics;
using System.Runtime.InteropServices;

namespace MicBridge;

internal static class NativeWindows
{
    public delegate bool EnumWindowsProc(nint hwnd, nint lParam);

    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc callback, nint lParam);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(nint hwnd);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(nint hwnd, out uint processId);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(nint hwnd, out Rect rect);
    [DllImport("user32.dll")] public static extern bool IsHungAppWindow(nint hwnd);

    [StructLayout(LayoutKind.Sequential)]
    public struct Rect { public int Left, Top, Right, Bottom; }

    public static nint FindDiscordWindow()
    {
        var pids = Process.GetProcessesByName("Discord").Select(p => (uint)p.Id).ToHashSet();
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
