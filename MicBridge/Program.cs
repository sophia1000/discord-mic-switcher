namespace MicBridge;

using System.IO;

internal static class Program
{
    [STAThread]
    private static int Main(string[] args)
    {
        if (args.Contains("--self-test", StringComparer.OrdinalIgnoreCase))
            return RunSelfTests();
        ApplicationConfiguration.Initialize();
        using var singleInstance = new Mutex(true, @"Local\MicBridge.SingleInstance", out bool isFirstInstance);
        if (!isFirstInstance)
        {
            MessageBox.Show("Sophia's Mic Bridge is already running.", "Sophia's Mic Bridge", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return 3;
        }
        var store = new SettingsStore();
        var settings = store.Load();
        int diagnosticIndex = Array.FindIndex(args, value => value.Equals("--diagnostic", StringComparison.OrdinalIgnoreCase));
        if (diagnosticIndex >= 0)
        {
            string output = diagnosticIndex + 1 < args.Length ? args[diagnosticIndex + 1] : Path.Combine(Environment.CurrentDirectory, "native-diagnostic.txt");
            var log = new AppLog(store, () => true);
            using var monitor = new DiscordMonitor(settings, log);
            monitor.Start();
            Thread.Sleep(8000);
            File.WriteAllText(output, $"ready={monitor.Ready}{Environment.NewLine}muted={monitor.Muted}{Environment.NewLine}deafened={monitor.Deafened}{Environment.NewLine}");
            return monitor.Ready ? 0 : 2;
        }
        using var coordinator = new SyncCoordinator(settings, store);
        Application.Run(new MainForm(coordinator, settings, store));
        return 0;
    }

    private static int RunSelfTests()
    {
        try
        {
            foreach (bool value in new[] { false, true })
            {
                byte[] packet = OscBridge.Build("/avatar/parameters/Test", value);
                if (!OscBridge.TryParse(packet, out string address, out bool parsed)
                    || address != "/avatar/parameters/Test" || parsed != value) return 11;
            }
            foreach (int value in new[] { 0, 1 })
            {
                byte[] packet = OscBridge.BuildInt("/input/Voice", value);
                if (!OscBridge.TryParse(packet, out string address, out bool parsed)
                    || address != "/input/Voice" || parsed != (value != 0)) return 16;
            }
            var oscSettings = new AppSettings { OscConnectionMode = OscConnectionMode.OscQuery, LoggingEnabled = false };
            var store = new SettingsStore();
            using (var bridge = new OscBridge(oscSettings, new AppLog(store, () => false)))
            {
                bridge.Start();
                if (!bridge.Connected || !bridge.ConnectionDescription.StartsWith("OSCQuery", StringComparison.Ordinal)) return 13;
            }
            return 0;
        }
        catch { return 12; }
    }
}
