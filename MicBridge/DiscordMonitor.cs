using FlaUI.Core.AutomationElements;
using FlaUI.Core.Definitions;
using FlaUI.UIA3;

namespace MicBridge;

public sealed class DiscordMonitor : IDisposable
{
    private readonly AppSettings _settings;
    private readonly AppLog _log;
    private readonly object _gate = new();
    private readonly CancellationTokenSource _stop = new();
    private Thread? _thread;
    private AutomationElement? _root;
    private AutomationElement? _muteButton;
    private AutomationElement? _deafenButton;
    private UIA3Automation? _automation;
    private nint _hwnd;
    private DateTime _attachedAt;
    private DateTime _scannedAt;
    private bool? _rawMute, _rawDeafen;
    private int _muteStreak, _deafenStreak;
    private bool? _muted, _deafened;
    private bool? _expectedMute, _expectedDeafen;
    private DateTime _expectedMuteUntil, _expectedDeafenUntil;

    public event Action<BridgeSignal>? Signal;
    public event Action? StatusChanged;
    public bool Ready { get; private set; }
    public bool CommandsSuspended { get; set; }
    public bool? Muted { get { lock (_gate) return _muted; } }
    public bool? Deafened { get { lock (_gate) return _deafened; } }

    public DiscordMonitor(AppSettings settings, AppLog log) { _settings = settings; _log = log; }

    public void Start()
    {
        _thread = new Thread(PollLoop) { IsBackground = true, Name = "Discord UI Automation" };
        // UI Automation calls into Chromium are most reliable from MTA.
        _thread.SetApartmentState(ApartmentState.MTA);
        _thread.Start();
    }

    private void PollLoop()
    {
        _log.Write("Discord UI Automation monitor started");
        _automation = new UIA3Automation();
        while (!_stop.IsCancellationRequested)
        {
            try
            {
                var (mute, deafen) = ReadStates();
                (_rawMute, _muteStreak) = Debounce(mute, _rawMute, _muteStreak);
                (_rawDeafen, _deafenStreak) = Debounce(deafen, _rawDeafen, _deafenStreak);
                bool ready = mute.HasValue && deafen.HasValue;
                if (ready != Ready) { Ready = ready; StatusChanged?.Invoke(); }
                Accept(_muteStreak >= 3 ? _rawMute : null, _deafenStreak >= 3 ? _rawDeafen : null);
            }
            catch (Exception ex)
            {
                if (Ready) { Ready = false; StatusChanged?.Invoke(); }
                _log.Write("Discord UIA read failed: " + ex.Message);
                _root = null;
            }
            _stop.Token.WaitHandle.WaitOne(Math.Clamp(_settings.DiscordPollMs, 50, 5000));
        }
        _automation.Dispose();
        _automation = null;
    }

    private static (bool?, int) Debounce(bool? raw, bool? previous, int streak)
    {
        if (!raw.HasValue) return (previous, 0);
        return !previous.HasValue || raw != previous ? (raw, 1) : (previous, streak + 1);
    }

    private (bool? mute, bool? deafen) ReadStates()
    {
        if (_root is null || DateTime.UtcNow - _attachedAt > TimeSpan.FromSeconds(10)) Attach();
        if (_root is null || _hwnd == 0 || NativeWindows.IsHungAppWindow(_hwnd)) return (null, null);
        if (_muteButton is null || _deafenButton is null ||
            DateTime.UtcNow - _scannedAt > TimeSpan.FromSeconds(_settings.DiscordRescanSeconds)) Scan();
        bool? mute = ReadToggle(_muteButton);
        bool? deafen = ReadToggle(_deafenButton);
        if (!mute.HasValue || !deafen.HasValue)
        {
            Scan();
            mute = ReadToggle(_muteButton);
            deafen = ReadToggle(_deafenButton);
        }
        return (mute, deafen);
    }

    private void Attach()
    {
        _hwnd = NativeWindows.FindDiscordWindow();
        _root = _hwnd == 0 ? null : _automation?.FromHandle(_hwnd);
        _attachedAt = DateTime.UtcNow;
        _scannedAt = DateTime.MinValue;
        _muteButton = _deafenButton = null;
        _log.Write($"Discord UIA attach: hwnd={_hwnd}, found={_root is not null}");
        if (_root is not null) Thread.Sleep(50);
    }

    private void Scan()
    {
        if (_root is null) return;
        _muteButton = _deafenButton = null;
        var started = DateTime.UtcNow;
        var buttons = _root.FindAllDescendants(cf => cf.ByControlType(ControlType.Button));
        foreach (AutomationElement button in buttons)
        {
            string name;
            try
            {
                name = button.Name?.Trim() ?? "";
                if (!button.IsEnabled || button.IsOffscreen) continue;
            }
            catch { continue; }
            if (_muteButton is null && _settings.DiscordMuteNames.Contains(name, StringComparer.OrdinalIgnoreCase)
                && SupportsToggle(button)) _muteButton = button;
            if (_deafenButton is null && _settings.DiscordDeafenNames.Contains(name, StringComparer.OrdinalIgnoreCase)
                && SupportsToggle(button)) _deafenButton = button;
            if (_muteButton is not null && _deafenButton is not null) break;
        }
        _scannedAt = DateTime.UtcNow;
        _log.Write($"Discord UI buttons: count={buttons.Length}, mute={_muteButton is not null}, deafen={_deafenButton is not null}, elapsed={(DateTime.UtcNow - started).TotalMilliseconds:F0}ms");
    }

    private static bool SupportsToggle(AutomationElement element) => element.Patterns.Toggle.IsSupported;

    private static bool? ReadToggle(AutomationElement? element)
    {
        try
        {
            if (element is not null && element.Patterns.Toggle.IsSupported)
                return element.Patterns.Toggle.Pattern.ToggleState.Value == ToggleState.On;
        }
        catch { }
        return null;
    }

    private void Accept(bool? mute, bool? deafen)
    {
        var events = new List<BridgeSignal>(2);
        lock (_gate)
        {
            if (deafen.HasValue && deafen != _deafened)
            {
                bool initial = !_deafened.HasValue;
                _deafened = deafen;
                bool expected = _expectedDeafen == deafen && DateTime.UtcNow <= _expectedDeafenUntil;
                _expectedDeafen = null;
                events.Add(new(SignalKind.DiscordDeafen, deafen.Value, initial, expected));
            }
            if (mute.HasValue && mute != _muted)
            {
                bool initial = !_muted.HasValue;
                _muted = mute;
                bool expected = _expectedMute == mute && DateTime.UtcNow <= _expectedMuteUntil;
                _expectedMute = null;
                events.Add(new(SignalKind.DiscordMute, mute.Value, initial, expected));
            }
        }
        if (events.Count > 0) StatusChanged?.Invoke();
        foreach (var item in events)
        {
            _log.Write($"{item.Kind} -> {item.Value} expected={item.Expected}");
            Signal?.Invoke(item);
        }
    }

    public bool SetMute(bool target)
    {
        lock (_gate)
        {
            if (CommandsSuspended || !Ready || !_muted.HasValue || _muted == target || string.IsNullOrWhiteSpace(_settings.MuteHotkey)) return false;
            if (_expectedMute.HasValue && DateTime.UtcNow <= _expectedMuteUntil)
            {
                _log.Write($"Discord mute hotkey suppressed: command for {_expectedMute.Value} is still pending");
                return _expectedMute == target;
            }
            _expectedMute = target;
            _expectedMuteUntil = DateTime.UtcNow.AddSeconds(4);
            if (TryToggleButton(_muteButton, "mute")) return true;
            if (NativeKeyboard.SendHotkey(_settings.MuteHotkey))
            {
                _log.Write($"Discord mute hotkey sent: {_settings.MuteHotkey}");
                return true;
            }
            _log.Write($"Discord mute hotkey failed: Windows error {NativeKeyboard.LastSendError}");
            _expectedMute = null;
            return false;
        }
    }

    public bool SetDeafen(bool target)
    {
        lock (_gate)
        {
            if (CommandsSuspended || !Ready || !_deafened.HasValue || _deafened == target || string.IsNullOrWhiteSpace(_settings.DeafenHotkey)) return false;
            if (_expectedDeafen.HasValue && DateTime.UtcNow <= _expectedDeafenUntil)
            {
                _log.Write($"Discord deafen hotkey suppressed: command for {_expectedDeafen.Value} is still pending");
                return _expectedDeafen == target;
            }
            _expectedDeafen = target;
            _expectedDeafenUntil = DateTime.UtcNow.AddSeconds(4);
            if (TryToggleButton(_deafenButton, "deafen")) return true;
            if (NativeKeyboard.SendHotkey(_settings.DeafenHotkey))
            {
                _log.Write($"Discord deafen hotkey sent: {_settings.DeafenHotkey}");
                return true;
            }
            _log.Write($"Discord deafen hotkey failed: Windows error {NativeKeyboard.LastSendError}");
            _expectedDeafen = null;
            return false;
        }
    }

    private bool TryToggleButton(AutomationElement? button, string command)
    {
        try
        {
            if (button is null || !button.Patterns.Toggle.IsSupported) return false;
            button.Patterns.Toggle.Pattern.Toggle();
            _log.Write($"Discord {command} toggled through accessibility control");
            return true;
        }
        catch (Exception ex)
        {
            _log.Write($"Discord {command} accessibility toggle failed; trying hotkey: {ex.Message}");
            return false;
        }
    }

    public void Dispose()
    {
        _stop.Cancel();
        _thread?.Join(1500);
        _stop.Dispose();
    }
}
