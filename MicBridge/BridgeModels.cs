namespace MicBridge;

using System.IO;

public enum SignalKind { DiscordMute, DiscordDeafen, VrchatMute, VrchatDeafen, VrchatToggle }
public readonly record struct BridgeSignal(SignalKind Kind, bool Value, bool Initial, bool Expected = false);

public sealed class AppLog
{
    private readonly SettingsStore _store;
    private readonly Func<bool> _enabled;
    private readonly object _gate = new();
    public AppLog(SettingsStore store, Func<bool> enabled) { _store = store; _enabled = enabled; }
    public void Write(string message)
    {
        if (!_enabled()) return;
        try
        {
            lock (_gate)
                File.AppendAllText(_store.LogPath, $"{DateTime.Now:yyyy-MM-dd HH:mm:ss.fff} | {message}{Environment.NewLine}");
        }
        catch { }
    }
}
