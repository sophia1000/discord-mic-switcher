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

internal static class NativeKeyboard
{
    private const uint InputKeyboard = 1;
    private const uint KeyUp = 0x0002;

    [StructLayout(LayoutKind.Sequential)]
    private struct Input { public uint Type; public InputUnion Data; }
    // INPUT's union is 32 bytes on x64 because MOUSEINPUT is larger than
    // KEYBDINPUT. SendInput rejects the structure if cbSize is not exactly 40.
    [StructLayout(LayoutKind.Explicit, Size = 32)]
    private struct InputUnion { [FieldOffset(0)] public KeyboardInput Keyboard; }
    [StructLayout(LayoutKind.Sequential)]
    private struct KeyboardInput
    {
        public ushort VirtualKey;
        public ushort ScanCode;
        public uint Flags;
        public uint Time;
        public nuint ExtraInfo;
    }

    [DllImport("user32.dll", SetLastError = true)]
    private static extern uint SendInput(uint count, Input[] inputs, int size);

    public static int LastSendError { get; private set; }
    internal static int InputStructureSize => Marshal.SizeOf<Input>();

    public static bool SendHotkey(string text)
    {
        var keys = Parse(text);
        if (keys.Count == 0) { LastSendError = 87; return false; }
        Input[] down = keys.Select(key => Make(key, false)).ToArray();
        Input[] up = keys.AsEnumerable().Reverse().Select(key => Make(key, true)).ToArray();

        uint pressed = SendInput((uint)down.Length, down, InputStructureSize);
        int pressError = pressed == down.Length ? 0 : Marshal.GetLastWin32Error();
        // Electron's global shortcut handler can miss a chord whose down/up
        // events are delivered in one zero-duration SendInput batch.
        Thread.Sleep(60);
        uint released = SendInput((uint)up.Length, up, InputStructureSize);
        int releaseError = released == up.Length ? 0 : Marshal.GetLastWin32Error();
        LastSendError = pressError != 0 ? pressError : releaseError;
        return pressed == down.Length && released == up.Length;
    }

    private static Input Make(ushort key, bool up) => new()
    {
        Type = InputKeyboard,
        Data = new InputUnion { Keyboard = new KeyboardInput { VirtualKey = key, Flags = up ? KeyUp : 0 } }
    };

    public static List<ushort> Parse(string text)
    {
        var result = new List<ushort>();
        foreach (string raw in (text ?? "").Split('+', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
        {
            string token = raw.ToLowerInvariant().Replace("left ", "").Replace("right ", "");
            Keys key = token switch
            {
                "ctrl" or "control" => Keys.ControlKey,
                "shift" => Keys.ShiftKey,
                "alt" => Keys.Menu,
                "win" or "windows" => Keys.LWin,
                "escape" => Keys.Escape,
                "enter" or "return" => Keys.Enter,
                "space" => Keys.Space,
                "page up" or "pageup" => Keys.PageUp,
                "page down" or "pagedown" => Keys.PageDown,
                _ => ParseKey(token)
            };
            if (key == Keys.None) return [];
            ushort vk = (ushort)key;
            if (!result.Contains(vk)) result.Add(vk);
        }
        return result;
    }

    private static Keys ParseKey(string token)
    {
        if (token.Length == 1 && char.IsLetter(token[0])) return (Keys)((int)Keys.A + char.ToUpperInvariant(token[0]) - 'A');
        if (token.Length == 1 && char.IsDigit(token[0])) return (Keys)((int)Keys.D0 + token[0] - '0');
        return Enum.TryParse<Keys>(token.Replace(" ", ""), true, out var key) ? key : Keys.None;
    }

    public static string DisplayName(Keys key) => key switch
    {
        Keys.LControlKey or Keys.RControlKey or Keys.ControlKey => "ctrl",
        Keys.LShiftKey or Keys.RShiftKey or Keys.ShiftKey => "shift",
        Keys.LMenu or Keys.RMenu or Keys.Menu => "alt",
        Keys.LWin or Keys.RWin => "windows",
        Keys.Escape => "escape",
        Keys.Return => "enter",
        >= Keys.A and <= Keys.Z => key.ToString().ToLowerInvariant(),
        >= Keys.D0 and <= Keys.D9 => ((int)key - (int)Keys.D0).ToString(),
        _ => key.ToString().ToLowerInvariant()
    };
}

internal sealed class GlobalKeyboardRecorder : IDisposable
{
    private const int WhKeyboardLl = 13;
    private const int WmKeyDown = 0x0100;
    private const int WmSysKeyDown = 0x0104;
    private readonly HookProc _proc;
    private nint _hook;
    public event Action<Keys>? KeyPressed;

    private delegate nint HookProc(int code, nint wParam, nint lParam);
    [DllImport("user32.dll")] private static extern nint SetWindowsHookEx(int id, HookProc proc, nint module, uint threadId);
    [DllImport("user32.dll")] private static extern bool UnhookWindowsHookEx(nint hook);
    [DllImport("user32.dll")] private static extern nint CallNextHookEx(nint hook, int code, nint wParam, nint lParam);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] private static extern nint GetModuleHandle(string? name);

    public GlobalKeyboardRecorder()
    {
        _proc = Callback;
        _hook = SetWindowsHookEx(WhKeyboardLl, _proc, GetModuleHandle(null), 0);
        if (_hook == 0) throw new InvalidOperationException("Windows could not start the keyboard recorder.");
    }

    private nint Callback(int code, nint wParam, nint lParam)
    {
        if (code >= 0 && (wParam == WmKeyDown || wParam == WmSysKeyDown))
        {
            int vk = Marshal.ReadInt32(lParam);
            KeyPressed?.Invoke((Keys)vk);
        }
        return CallNextHookEx(_hook, code, wParam, lParam);
    }

    public void Dispose()
    {
        if (_hook != 0) UnhookWindowsHookEx(_hook);
        _hook = 0;
        GC.SuppressFinalize(this);
    }
}
