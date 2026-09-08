using FlaUI.Core.AutomationElements;
using FlaUI.Core.Definitions;
using FlaUI.UIA3;

namespace MicBridge;

public sealed class DiscordMonitor : IDisposable
{
    private const int StableReadCount = 2;
    private const int MaxCommandAttempts = 5;
    private static readonly TimeSpan CommandTimeout = TimeSpan.FromSeconds(6);
    private static readonly TimeSpan RetryDelay = TimeSpan.FromMilliseconds(750);

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
    private DateTime _nextAttachAt, _nextScanAt;
    private bool? _rawMute, _rawDeafen;
    private int _muteStreak, _deafenStreak;
    private bool? _muted, _deafened;
    private bool? _expectedMute, _expectedDeafen;
    private DateTime _expectedMuteUntil, _expectedDeafenUntil;
    private DateTime _nextMuteCommandAt, _nextDeafenCommandAt;
    private int _muteCommandAttempts, _deafenCommandAttempts;

    public event Action<BridgeSignal>? Signal;
    public event Action? StatusChanged;
    public bool Ready => MuteReady;
    public bool MuteReady { get; private set; }
    public bool DeafenReady { get; private set; }
    public bool? Muted { get { lock (_gate) return _muted; } }
    public bool? Deafened { get { lock (_gate) return _deafened; } }

    public DiscordMonitor(AppSettings settings, AppLog log) { _settings = settings; _log = log; }

    public void Start()
    {
        _thread = new Thread(PollLoop) { IsBackground = true, Name = "Discord UI Automation" };
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

                bool muteReady = mute.HasValue;
                bool deafenReady = deafen.HasValue;
                if (muteReady != MuteReady || deafenReady != DeafenReady)
                {
                    MuteReady = muteReady;
                    DeafenReady = deafenReady;
                    StatusChanged?.Invoke();
                }

                bool muteAcknowledged, deafenAcknowledged;
                lock (_gate)
                {
                    muteAcknowledged = mute.HasValue && _expectedMute == mute;
                    deafenAcknowledged = deafen.HasValue && _expectedDeafen == deafen;
                }
                Accept(muteAcknowledged ? mute : _muteStreak >= StableReadCount ? _rawMute : null,
                    deafenAcknowledged ? deafen : _deafenStreak >= StableReadCount ? _rawDeafen : null);
                RetryPendingCommands();
            }
            catch (Exception ex)
            {
                if (MuteReady || DeafenReady)
                {
                    MuteReady = DeafenReady = false;
                    StatusChanged?.Invoke();
                }
                _log.Write("Discord UIA read failed: " + ex.Message);
                InvalidateRoot();
            }
            _stop.Token.WaitHandle.WaitOne(Math.Clamp(_settings.DiscordPollMs, 40, 5000));
        }
        _automation.Dispose();
        _automation = null;
    }

    private static (bool?, int) Debounce(bool? raw, bool? previous, int streak)
    {
        if (!raw.HasValue) return (previous, 0);
        return !previous.HasValue || raw != previous ? (raw, 1) : (previous, Math.Min(streak + 1, StableReadCount));
    }

    private (bool? mute, bool? deafen) ReadStates()
    {
        if (_root is null || _hwnd == 0 || !NativeWindows.IsWindow(_hwnd))
        {
            if (DateTime.UtcNow < _nextAttachAt) return (null, null);
            _nextAttachAt = DateTime.UtcNow.AddSeconds(1);
            Attach();
        }
        if (_root is null || _hwnd == 0 || NativeWindows.IsHungAppWindow(_hwnd)) return (null, null);
        if (NativeWindows.RestoreDiscordBehindOtherWindows(_hwnd))
        {
            _log.Write("Discord was minimized; restored without activation and moved behind other windows");
            InvalidateRoot();
            _stop.Token.WaitHandle.WaitOne(250);
            return (null, null);
        }

        AutomationElement? muteButton, deafenButton;
        lock (_gate) { muteButton = _muteButton; deafenButton = _deafenButton; }
        if (muteButton is null || deafenButton is null)
        {
            Scan();
            lock (_gate) { muteButton = _muteButton; deafenButton = _deafenButton; }
        }

        bool? mute = ReadToggle(muteButton);
        bool? deafen = ReadToggle(deafenButton);
        if (!mute.HasValue || !deafen.HasValue)
        {
            lock (_gate)
            {
                if (!mute.HasValue) _muteButton = null;
                if (!deafen.HasValue) _deafenButton = null;
            }
            Scan();
            lock (_gate) { muteButton = _muteButton; deafenButton = _deafenButton; }
            mute = ReadToggle(muteButton);
            deafen = ReadToggle(deafenButton);
        }
        return (mute, deafen);
    }

    private void Attach()
    {
        nint hwnd = NativeWindows.FindDiscordWindow();
        AutomationElement? root = hwnd == 0 ? null : _automation?.FromHandle(hwnd);
        _hwnd = hwnd;
        _root = root;
        lock (_gate) { _muteButton = _deafenButton = null; }
        _log.Write($"Discord UIA attach: hwnd={_hwnd}, found={_root is not null}");
    }

    private void InvalidateRoot()
    {
        _root = null;
        _hwnd = 0;
        lock (_gate) { _muteButton = _deafenButton = null; }
    }

    private void Scan()
    {
        if (_root is null || DateTime.UtcNow < _nextScanAt) return;
        _nextScanAt = DateTime.UtcNow.AddMilliseconds(500);
        var started = DateTime.UtcNow;
        AutomationElement? mute = _muteButton ?? FindNamedToggle(_settings.DiscordMuteNames);
        AutomationElement? deafen = _deafenButton ?? FindNamedToggle(_settings.DiscordDeafenNames);
        lock (_gate)
        {
            if (mute is not null) _muteButton = mute;
            if (deafen is not null) _deafenButton = deafen;
        }
        _log.Write($"Discord UI targeted scan: mute={mute is not null}, deafen={deafen is not null}, elapsed={(DateTime.UtcNow - started).TotalMilliseconds:F0}ms");
    }

    private AutomationElement? FindNamedToggle(IEnumerable<string> names)
    {
        foreach (string name in names.Where(value => !string.IsNullOrWhiteSpace(value)))
        {
            try
            {
                var element = _root?.FindFirstDescendant(cf =>
                    cf.ByControlType(ControlType.Button).And(cf.ByName(name)));
                if (element is not null && element.IsEnabled && element.Patterns.Toggle.IsSupported) return element;
            }
            catch { }
        }
        return null;
    }

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
                _deafenCommandAttempts = 0;
                events.Add(new(SignalKind.DiscordDeafen, deafen.Value, initial, expected));
            }
            if (mute.HasValue && mute != _muted)
            {
                bool initial = !_muted.HasValue;
                _muted = mute;
                bool expected = _expectedMute == mute && DateTime.UtcNow <= _expectedMuteUntil;
                _expectedMute = null;
                _muteCommandAttempts = 0;
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

    public bool SetMute(bool target) => QueueCommand(target, true);
    public bool SetDeafen(bool target) => QueueCommand(target, false);

    private bool QueueCommand(bool target, bool muteCommand)
    {
        DateTime now = DateTime.UtcNow;
        lock (_gate)
        {
            bool ready = muteCommand ? MuteReady : DeafenReady;
            bool? current = muteCommand ? _muted : _deafened;
            bool? expected = muteCommand ? _expectedMute : _expectedDeafen;
            DateTime expectedUntil = muteCommand ? _expectedMuteUntil : _expectedDeafenUntil;
            if (!ready || !current.HasValue || current == target) return false;
            if (expected.HasValue && now <= expectedUntil)
            {
                _log.Write($"Discord {(muteCommand ? "mute" : "deafen")} command coalesced with pending target {expected.Value}");
                return expected == target;
            }

            if (muteCommand)
            {
                _expectedMute = target;
                _expectedMuteUntil = now + CommandTimeout;
                _muteCommandAttempts = 0;
                _nextMuteCommandAt = now;
            }
            else
            {
                _expectedDeafen = target;
                _expectedDeafenUntil = now + CommandTimeout;
                _deafenCommandAttempts = 0;
                _nextDeafenCommandAt = now;
            }
        }

        // All accessibility commands execute on the polling thread, never on
        // the OSC receiver or UI thread. The first attempt follows a fresh read.
        return true;
    }

    private void RetryPendingCommands()
    {
        AutomationElement? muteButton = null, deafenButton = null;
        int muteAttempt = 0, deafenAttempt = 0;
        DateTime now = DateTime.UtcNow;
        lock (_gate)
        {
            if (!_settings.SystemEnabled || _deafened != false || _rawDeafen != false)
                _expectedMute = null;
            if (!_settings.DeafenEnabled) _expectedDeafen = null;
            if (_expectedMute == _muted && _rawMute == _muted) _expectedMute = null;
            if (_expectedDeafen == _deafened && _rawDeafen == _deafened) _expectedDeafen = null;
            if (_expectedMute.HasValue)
            {
                if (now > _expectedMuteUntil || _muteCommandAttempts >= MaxCommandAttempts)
                {
                    _log.Write($"Discord mute command failed after {_muteCommandAttempts} verified attempts");
                    _expectedMute = null;
                }
                else if (now >= _nextMuteCommandAt && _muteButton is not null)
                {
                    muteButton = _muteButton;
                    muteAttempt = ++_muteCommandAttempts;
                    _nextMuteCommandAt = now + RetryDelay;
                }
            }
            if (_expectedDeafen.HasValue)
            {
                if (now > _expectedDeafenUntil || _deafenCommandAttempts >= MaxCommandAttempts)
                {
                    _log.Write($"Discord deafen command failed after {_deafenCommandAttempts} verified attempts");
                    _expectedDeafen = null;
                }
                else if (now >= _nextDeafenCommandAt && _deafenButton is not null)
                {
                    deafenButton = _deafenButton;
                    deafenAttempt = ++_deafenCommandAttempts;
                    _nextDeafenCommandAt = now + RetryDelay;
                }
            }
        }
        if (muteButton is not null) TryToggleButton(muteButton, true, muteAttempt);
        if (deafenButton is not null) TryToggleButton(deafenButton, false, deafenAttempt);
    }

    private bool TryToggleButton(AutomationElement button, bool muteCommand, int attempt)
    {
        string command = muteCommand ? "mute" : "deafen";
        try
        {
            if (muteCommand && (!_settings.SystemEnabled || ReadToggle(_deafenButton) != false)) return false;
            if (!muteCommand && !_settings.DeafenEnabled) return false;
            if (!button.Patterns.Toggle.IsSupported) throw new InvalidOperationException("Toggle pattern is unavailable");
            button.Patterns.Toggle.Pattern.Toggle();
            _log.Write($"Discord {command} accessibility command sent (attempt {attempt})");
            return true;
        }
        catch (Exception ex)
        {
            lock (_gate)
            {
                if (muteCommand)
                {
                    _muteButton = null;
                    _nextMuteCommandAt = DateTime.UtcNow.AddMilliseconds(50);
                }
                else
                {
                    _deafenButton = null;
                    _nextDeafenCommandAt = DateTime.UtcNow.AddMilliseconds(50);
                }
            }
            _log.Write($"Discord {command} accessibility attempt {attempt} failed: {ex.Message}");
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
