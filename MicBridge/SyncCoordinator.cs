namespace MicBridge;

public sealed class SyncCoordinator : IDisposable
{
    private readonly AppSettings _settings;
    private readonly SettingsStore _store;
    private readonly AppLog _log;
    private readonly object _gate = new();
    private string _oscSignature = "";

    public DiscordMonitor Discord { get; }
    public OscBridge Vrchat { get; }
    public string LastAction { get; private set; } = "Starting native bridge…";
    public event Action? Updated;

    public SyncCoordinator(AppSettings settings, SettingsStore store)
    {
        _settings = settings;
        _store = store;
        _log = new AppLog(store, () => _settings.LoggingEnabled);
        Discord = new DiscordMonitor(settings, _log);
        Vrchat = new OscBridge(settings, _log);
        Discord.Signal += OnSignal;
        Vrchat.Signal += OnSignal;
        Discord.StatusChanged += Notify;
        Vrchat.StatusChanged += Notify;
        _oscSignature = OscSignature();
        Vrchat.Start();
        Discord.Start();
    }

    private void OnSignal(BridgeSignal signal)
    {
        lock (_gate)
        {
            if (signal.Kind == SignalKind.VrchatToggle)
            {
                if (!signal.Initial)
                {
                    _settings.SystemEnabled = signal.Value;
                    _store.Save(_settings);
                    LastAction = signal.Value ? "Mute sync enabled by VRChat parameter" : "Mute sync disabled by VRChat parameter";
                    if (signal.Value) SynchronizeAll();
                }
                Notify();
                return;
            }
            if (signal.Expected) { Notify(); return; }
            if (signal.Kind is SignalKind.DiscordDeafen or SignalKind.VrchatDeafen)
                HandleDeafen(signal);
            else
                HandleMute(signal);
        }
        Notify();
    }

    private void HandleMute(BridgeSignal signal)
    {
        if (!_settings.SystemEnabled) { LastAction = "Mute change observed; sync is paused"; return; }
        if (Discord.Deafened == true) { LastAction = "Mic sync paused while Discord is deafened"; return; }
        if (_settings.MuteMode == SyncMode.Dynamic)
        {
            if (signal.Initial) { LastAction = "Mute bridge ready — waiting for either app to change"; return; }
            if (signal.Kind == SignalKind.DiscordMute) SetVrchatMute(!signal.Value, "Discord changed");
            else SetDiscordMute(!signal.Value, "VRChat changed");
        }
        else if (_settings.MuteMode == SyncMode.VrchatMaster)
        {
            if (Vrchat.MuteFound && Vrchat.Muted.HasValue) SetDiscordMute(!Vrchat.Muted.Value, "VRChat is master");
        }
        else if (Vrchat.MuteFound && Discord.Muted.HasValue)
            SetVrchatMute(!Discord.Muted.Value, "Discord is master");
    }

    private void HandleDeafen(BridgeSignal signal)
    {
        if (!_settings.DeafenEnabled)
        {
            LastAction = signal.Kind == SignalKind.DiscordDeafen && signal.Value
                ? "Mic sync paused while Discord is deafened"
                : "Deafen changed; parameter bridge is off";
            return;
        }
        if (_settings.DeafenMode == SyncMode.Dynamic)
        {
            if (signal.Initial) { LastAction = "Deafen bridge ready — waiting for either side to change"; return; }
            if (signal.Kind == SignalKind.DiscordDeafen) SetVrchatDeafen(signal.Value, "Discord deafen changed");
            else SetDiscordDeafen(signal.Value, "VRChat deafen changed");
        }
        else if (_settings.DeafenMode == SyncMode.VrchatMaster)
        {
            if (Vrchat.DeafenFound && Vrchat.Deafened.HasValue) SetDiscordDeafen(Vrchat.Deafened.Value, "VRChat deafen is master");
        }
        else if (Vrchat.DeafenFound && Discord.Deafened.HasValue)
            SetVrchatDeafen(Discord.Deafened.Value, "Discord deafen is master");
    }

    private void SetDiscordMute(bool target, string reason)
    {
        bool alreadyCorrect = Discord.Muted == target;
        bool sent = Discord.SetMute(target);
        LastAction = !Discord.Ready ? "Waiting for Discord accessibility state"
            : $"{reason} → Discord {(target ? "muted" : "live")}{(sent ? "" : alreadyCorrect ? " (already correct)" : " (command failed)")}";
        _log.Write(LastAction);
    }

    private void SetVrchatMute(bool target, string reason)
    {
        bool alreadyCorrect = Vrchat.Muted == target;
        bool sent = Vrchat.SetMute(target);
        LastAction = $"{reason} → VRChat {(target ? "muted" : "live")}{(sent ? "" : alreadyCorrect ? " (already correct)" : " (command failed)")}";
        _log.Write(LastAction);
    }

    private void SetDiscordDeafen(bool target, string reason)
    {
        bool sent = Discord.SetDeafen(target);
        LastAction = Discord.DeafenReady ? $"{reason} → Discord {(target ? "deafened" : "undeafened")}{(sent ? "" : " (already correct)")}" : "Waiting for Discord deafen state";
        _log.Write(LastAction);
    }

    private void SetVrchatDeafen(bool target, string reason)
    {
        bool sent = Vrchat.SetDeafen(target);
        LastAction = $"{reason} → VRChat {_settings.DeafenParameter}={target}{(sent ? "" : " (already correct)")}";
        _log.Write(LastAction);
    }

    public void SettingsChanged()
    {
        lock (_gate)
        {
            _store.Save(_settings);
            string signature = OscSignature();
            if (signature != _oscSignature)
            {
                _oscSignature = signature;
                Vrchat.Restart();
            }
            LastAction = "Settings saved automatically";
            SynchronizeAll();
        }
        Notify();
    }

    public void SynchronizeAll()
    {
        lock (_gate)
        {
            if (!_settings.SystemEnabled) LastAction = "Mute sync disabled";
            else if (Discord.Deafened == true) LastAction = "Mute sync paused while Discord is deafened";
            else if (_settings.MuteMode == SyncMode.VrchatMaster && Vrchat.MuteFound && Vrchat.Muted.HasValue)
                SetDiscordMute(!Vrchat.Muted.Value, "VRChat is master");
            else if (_settings.MuteMode == SyncMode.DiscordMaster && Vrchat.MuteFound && Discord.Muted.HasValue)
                SetVrchatMute(!Discord.Muted.Value, "Discord is master");

            if (!_settings.DeafenEnabled) return;
            if (!Vrchat.DeafenFound) LastAction = $"Waiting for VRChat {_settings.DeafenParameter}";
            else if (_settings.DeafenMode == SyncMode.VrchatMaster && Vrchat.Deafened.HasValue)
                SetDiscordDeafen(Vrchat.Deafened.Value, "VRChat deafen is master");
            else if (_settings.DeafenMode == SyncMode.DiscordMaster && Discord.Deafened.HasValue)
                SetVrchatDeafen(Discord.Deafened.Value, "Discord deafen is master");
        }
        Notify();
    }

    private string OscSignature() => $"{_settings.OscConnectionMode}|{_settings.OscReceivePort}|{_settings.OscSendPort}|{_settings.VrchatIp}|{_settings.MuteParameter}|{_settings.ToggleParameter}|{_settings.DeafenParameter}";
    private void Notify() => Updated?.Invoke();

    public void SetStatus(string message)
    {
        LastAction = message;
        Notify();
    }

    public void Dispose()
    {
        Discord.Dispose();
        Vrchat.Dispose();
    }
}
