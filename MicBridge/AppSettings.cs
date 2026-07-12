using System.Text.Json;
using System.Text.Json.Serialization;
using System.IO;

namespace MicBridge;

public enum SyncMode
{
    Dynamic,
    VrchatMaster,
    DiscordMaster
}

public enum OscConnectionMode
{
    OscQuery,
    Manual
}

public sealed class AppSettings
{
    [JsonPropertyName("system_enabled")] public bool SystemEnabled { get; set; } = true;
    [JsonPropertyName("sync_mode")] public SyncMode MuteMode { get; set; } = SyncMode.Dynamic;
    [JsonPropertyName("deafen_sync_enabled")] public bool DeafenEnabled { get; set; }
    [JsonPropertyName("deafen_sync_mode")] public SyncMode DeafenMode { get; set; } = SyncMode.Dynamic;
    [JsonPropertyName("discord_mute_hotkey")] public string MuteHotkey { get; set; } = "ctrl+shift+f12";
    [JsonPropertyName("discord_deafen_hotkey")] public string DeafenHotkey { get; set; } = "ctrl+shift+alt+f12";
    [JsonPropertyName("discord_poll_interval_ms")] public int DiscordPollMs { get; set; } = 100;
    [JsonPropertyName("discord_rescan_every_s")] public double DiscordRescanSeconds { get; set; } = 6;
    [JsonPropertyName("discord_mute_names")] public string[] DiscordMuteNames { get; set; } = ["Mute", "Unmute"];
    [JsonPropertyName("discord_deafen_names")] public string[] DiscordDeafenNames { get; set; } = ["Deafen", "Undeafen"];
    [JsonPropertyName("osc_connection_mode")] public OscConnectionMode OscConnectionMode { get; set; } = OscConnectionMode.OscQuery;
    [JsonPropertyName("vrchat_osc_ip")] public string VrchatIp { get; set; } = "127.0.0.1";
    [JsonPropertyName("vrchat_osc_send_port")] public int OscSendPort { get; set; } = 9000;
    [JsonPropertyName("vrchat_osc_receive_port")] public int OscReceivePort { get; set; } = 9123;
    [JsonPropertyName("vrchat_voice_press_ms")] public int VrchatVoicePressMs { get; set; } = 80;
    [JsonPropertyName("mute_parameter_name")] public string MuteParameter { get; set; } = "MuteSelf";
    [JsonPropertyName("toggle_parameter_name")] public string ToggleParameter { get; set; } = "ToggleMicSync";
    [JsonPropertyName("deafen_parameter_name")] public string DeafenParameter { get; set; } = "discorddeafen";
    [JsonPropertyName("logging_enabled")] public bool LoggingEnabled { get; set; } = true;
}

public sealed class SettingsStore
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        WriteIndented = true,
        PropertyNameCaseInsensitive = true,
        Converters = { new JsonStringEnumConverter(JsonNamingPolicy.SnakeCaseLower) }
    };

    public string DirectoryPath { get; } = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "MicBridge");
    public string SettingsPath => Path.Combine(DirectoryPath, "settings.json");
    public string LogPath => Path.Combine(DirectoryPath, "micbridge.log");

    public AppSettings Load()
    {
        Directory.CreateDirectory(DirectoryPath);
        string? source = File.Exists(SettingsPath) ? SettingsPath : FindLegacySettings();
        if (source is not null)
        {
            try
            {
                var loaded = JsonSerializer.Deserialize<AppSettings>(File.ReadAllText(source), JsonOptions);
                if (loaded is not null)
                {
                    Normalize(loaded);
                    Save(loaded);
                    return loaded;
                }
            }
            catch { }
        }
        var settings = new AppSettings();
        Save(settings);
        return settings;
    }

    private static string? FindLegacySettings()
    {
        string[] candidates =
        [
            Path.Combine(AppContext.BaseDirectory, "mic_sync_config.json"),
            Path.Combine(Environment.CurrentDirectory, "mic_sync_config.json")
        ];
        return candidates.FirstOrDefault(File.Exists);
    }

    public void Save(AppSettings settings)
    {
        Directory.CreateDirectory(DirectoryPath);
        Normalize(settings);
        string temp = SettingsPath + ".tmp";
        File.WriteAllText(temp, JsonSerializer.Serialize(settings, JsonOptions));
        File.Move(temp, SettingsPath, true);
    }

    private static void Normalize(AppSettings s)
    {
        s.DiscordPollMs = Math.Clamp(s.DiscordPollMs, 50, 5000);
        s.OscSendPort = Math.Clamp(s.OscSendPort, 1, 65535);
        s.OscReceivePort = Math.Clamp(s.OscReceivePort, 1, 65535);
        s.VrchatVoicePressMs = Math.Clamp(s.VrchatVoicePressMs, 20, 1000);
        s.VrchatIp = string.IsNullOrWhiteSpace(s.VrchatIp) ? "127.0.0.1" : s.VrchatIp.Trim();
        s.DiscordMuteNames ??= ["Mute", "Unmute"];
        s.DiscordDeafenNames ??= ["Deafen", "Undeafen"];
        s.MuteHotkey ??= "";
        s.DeafenHotkey ??= "";
        s.MuteParameter ??= "MuteSelf";
        s.ToggleParameter ??= "";
        s.DeafenParameter ??= "";
    }
}
