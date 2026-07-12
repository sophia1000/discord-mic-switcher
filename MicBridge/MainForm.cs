using System.Drawing;

namespace MicBridge;

public sealed class MainForm : Form
{
    private static readonly Color Bg = Color.FromArgb(11, 16, 32);
    private static readonly Color Panel = Color.FromArgb(21, 28, 47);
    private static readonly Color Panel2 = Color.FromArgb(28, 37, 64);
    private static readonly Color TextColor = Color.FromArgb(238, 242, 255);
    private static readonly Color MutedColor = Color.FromArgb(154, 166, 195);
    private static readonly Color Blue = Color.FromArgb(88, 101, 242);
    private static readonly Color Cyan = Color.FromArgb(45, 212, 191);
    private static readonly Color Red = Color.FromArgb(251, 113, 133);

    private readonly SyncCoordinator _coordinator;
    private readonly AppSettings _settings;
    private readonly SettingsStore _store;
    private readonly System.Windows.Forms.Timer _saveTimer = new() { Interval = 500 };
    private readonly System.Windows.Forms.Timer _refreshTimer = new() { Interval = 150 };
    private readonly System.Windows.Forms.Timer _recordFinishTimer = new() { Interval = 5000 };
    private readonly System.Windows.Forms.Timer _recordTimeoutTimer = new() { Interval = 30000 };
    private bool _loading = true;

    private readonly Label _discordState = NewLabel("WAITING", 22, true);
    private readonly Label _discordInfo = NewLabel("Searching Discord UI…", 9);
    private readonly Label _discordDeafen = NewLabel("Deafen: waiting", 9, true);
    private readonly Label _vrcState = NewLabel("WAITING", 22, true);
    private readonly Label _vrcConnection = NewLabel("Starting OSCQuery…", 8);
    private readonly Label _vrcInfo = NewLabel("Waiting for MuteSelf", 9);
    private readonly Label _vrcDeafen = NewLabel("Deafen parameter: off", 9, true);
    private readonly Label _action = NewLabel("Starting…", 9);
    private readonly CheckBox _systemEnabled = NewCheck("SYNC ON");
    private readonly CheckBox _deafenEnabled = NewCheck("ENABLE DEAFEN PARAMETER BRIDGE");
    private readonly Dictionary<SyncMode, RadioButton> _muteModes = [];
    private readonly Dictionary<SyncMode, RadioButton> _deafenModes = [];
    private readonly TextBox _muteHotkey = NewTextBox();
    private readonly TextBox _deafenHotkey = NewTextBox();
    private readonly TextBox _muteParameter = NewTextBox();
    private readonly TextBox _toggleParameter = NewTextBox();
    private readonly TextBox _deafenParameter = NewTextBox();
    private readonly ComboBox _oscMode = new() { DropDownStyle = ComboBoxStyle.DropDownList, BackColor = Panel2, ForeColor = TextColor, FlatStyle = FlatStyle.Flat };
    private readonly TextBox _vrchatIp = NewTextBox();
    private readonly NumericUpDown _sendPort = NewPort();
    private readonly NumericUpDown _receivePort = NewPort();
    private readonly CheckBox _logging = NewCheck("Write diagnostic log");

    private GlobalKeyboardRecorder? _recorder;
    private TextBox? _recordTarget;
    private Button? _recordButton;
    private readonly List<string> _recordedKeys = [];

    public MainForm(SyncCoordinator coordinator, AppSettings settings, SettingsStore store)
    {
        _coordinator = coordinator;
        _settings = settings;
        _store = store;
        Text = "Mic Bridge";
        ClientSize = new Size(840, 760);
        MinimumSize = new Size(780, 700);
        BackColor = Bg;
        ForeColor = TextColor;
        Font = new Font("Segoe UI", 10);
        StartPosition = FormStartPosition.CenterScreen;

        Controls.Add(BuildLayout());
        LoadSettingsIntoControls();
        WireEvents();
        _loading = false;
        RefreshStatus();
        _refreshTimer.Start();
    }

    private Control BuildLayout()
    {
        var root = new TableLayoutPanel { Dock = DockStyle.Fill, BackColor = Bg, Padding = new Padding(22), RowCount = 3 };
        root.RowStyles.Add(new RowStyle(SizeType.Absolute, 60));
        root.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        root.RowStyles.Add(new RowStyle(SizeType.Absolute, 34));

        var header = new Panel { Dock = DockStyle.Fill, BackColor = Bg };
        var title = NewLabel("MIC BRIDGE", 23, true); title.AutoSize = true; title.Location = new Point(0, 4);
        var sub = NewLabel("Native Discord  ↔  VRChat", 10); sub.AutoSize = true; sub.Location = new Point(190, 16);
        _systemEnabled.AutoSize = true; _systemEnabled.ForeColor = Cyan; _systemEnabled.Location = new Point(690, 12); _systemEnabled.Anchor = AnchorStyles.Top | AnchorStyles.Right;
        header.Controls.AddRange([title, sub, _systemEnabled]);
        root.Controls.Add(header, 0, 0);

        var tabs = new TabControl { Dock = DockStyle.Fill, Appearance = TabAppearance.Normal, Padding = new Point(16, 7) };
        tabs.TabPages.Add(BuildBridgePage());
        tabs.TabPages.Add(BuildSettingsPage());
        root.Controls.Add(tabs, 0, 1);

        _action.Dock = DockStyle.Fill; _action.TextAlign = ContentAlignment.MiddleLeft;
        root.Controls.Add(_action, 0, 2);
        return root;
    }

    private TabPage BuildBridgePage()
    {
        var page = NewPage("Bridge");
        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, BackColor = Bg, Padding = new Padding(12), RowCount = 4 };
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 175));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 175));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 205));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));

        var status = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, BackColor = Bg };
        status.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50));
        status.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50));
        status.Controls.Add(StatusCard("DISCORD", _discordState, _discordInfo, _discordDeafen), 0, 0);
        status.Controls.Add(StatusCard("VRCHAT", _vrcState, _vrcConnection, _vrcInfo, _vrcDeafen), 1, 0);
        layout.Controls.Add(status, 0, 0);
        layout.Controls.Add(DirectionCard("MUTE DIRECTION", _muteModes), 0, 1);

        var deafenCard = DirectionCard("DEAFEN PARAMETER DIRECTION", _deafenModes);
        _deafenEnabled.AutoSize = true; _deafenEnabled.ForeColor = Cyan; _deafenEnabled.Location = new Point(18, 42);
        deafenCard.Controls.Add(_deafenEnabled);
        foreach (var radio in _deafenModes.Values) radio.Top += 34;
        layout.Controls.Add(deafenCard, 0, 2);

        var hint = NewLabel("Mute states stay opposite. Deafen states match the custom VRChat boolean directly.", 9);
        hint.Dock = DockStyle.Top; hint.Padding = new Padding(4, 8, 0, 0);
        layout.Controls.Add(hint, 0, 3);
        page.Controls.Add(layout);
        return page;
    }

    private TabPage BuildSettingsPage()
    {
        var page = NewPage("Settings");
        var grid = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, BackColor = Bg, Padding = new Padding(20), ColumnCount = 2, RowCount = 6 };
        grid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50));
        grid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50));

        AddSetting(grid, 0, 0, "Discord mute keybind", KeybindEditor(_muteHotkey));
        AddSetting(grid, 1, 0, "Discord deafen keybind", KeybindEditor(_deafenHotkey));
        _oscMode.Items.AddRange(["OSCQuery (automatic)", "Manual IP and ports"]);
        AddSetting(grid, 0, 1, "VRChat connection", _oscMode);
        AddSetting(grid, 1, 1, "Manual VRChat IP", _vrchatIp);
        AddSetting(grid, 0, 2, "Manual OSC send port", _sendPort);
        AddSetting(grid, 1, 2, "Manual OSC receive port", _receivePort);
        AddSetting(grid, 0, 3, "VRChat mute parameter", _muteParameter);
        AddSetting(grid, 1, 3, "VRChat sync toggle parameter", _toggleParameter);
        AddSetting(grid, 0, 4, "VRChat deafen boolean parameter", _deafenParameter);
        var path = NewLabel("Settings: " + _store.SettingsPath, 8); path.AutoSize = true; path.Padding = new Padding(0, 12, 0, 0);
        grid.Controls.Add(path, 1, 4);
        _logging.AutoSize = true; _logging.ForeColor = TextColor; _logging.Padding = new Padding(0, 12, 0, 0);
        grid.Controls.Add(_logging, 0, 5);
        var auto = NewLabel("All changes save automatically. OSCQuery is recommended.", 9); auto.AutoSize = true; auto.ForeColor = Cyan; auto.Padding = new Padding(0, 12, 0, 0);
        grid.Controls.Add(auto, 1, 5);
        page.Controls.Add(grid);
        return page;
    }

    private Panel StatusCard(string title, params Label[] labels)
    {
        var panel = NewPanel(); panel.Margin = new Padding(5); panel.Padding = new Padding(18);
        var heading = NewLabel(title, 9, true); heading.AutoSize = true; heading.Location = new Point(18, 12);
        panel.Controls.Add(heading);
        int y = 35;
        foreach (var label in labels) { label.AutoSize = true; label.Location = new Point(18, y); panel.Controls.Add(label); y += label.Font.Size > 15 ? 40 : 23; }
        return panel;
    }

    private Panel DirectionCard(string title, Dictionary<SyncMode, RadioButton> destination)
    {
        var panel = NewPanel(); panel.Margin = new Padding(5); panel.Padding = new Padding(18);
        var heading = NewLabel(title, 9, true); heading.AutoSize = true; heading.Location = new Point(18, 14); panel.Controls.Add(heading);
        var items = new[] { (SyncMode.VrchatMaster, "VRChat master"), (SyncMode.Dynamic, "Dynamic"), (SyncMode.DiscordMaster, "Discord master") };
        int x = 18;
        foreach (var (mode, text) in items)
        {
            var radio = new RadioButton
            {
                Appearance = Appearance.Button, Text = text, TextAlign = ContentAlignment.MiddleCenter,
                FlatStyle = FlatStyle.Flat, BackColor = Panel2, ForeColor = MutedColor,
                Size = new Size(220, 54), Location = new Point(x, 52), Tag = mode, Cursor = Cursors.Hand
            };
            radio.FlatAppearance.BorderSize = 0;
            destination[mode] = radio; panel.Controls.Add(radio); x += 232;
        }
        return panel;
    }

    private Control KeybindEditor(TextBox target)
    {
        var panel = new TableLayoutPanel { Dock = DockStyle.Top, Height = 38, ColumnCount = 2, BackColor = Bg };
        panel.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 44));
        panel.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        var record = new Button { Text = "●", Dock = DockStyle.Fill, FlatStyle = FlatStyle.Flat, BackColor = Panel2, ForeColor = Red, Font = new Font("Segoe UI Symbol", 14, FontStyle.Bold), Cursor = Cursors.Hand, AccessibleName = "Record keybind" };
        record.FlatAppearance.BorderSize = 0;
        record.Click += (_, _) => StartRecording(target, record);
        target.Dock = DockStyle.Fill;
        panel.Controls.Add(record, 0, 0); panel.Controls.Add(target, 1, 0);
        return panel;
    }

    private static void AddSetting(TableLayoutPanel grid, int column, int row, string caption, Control editor)
    {
        var box = new Panel { Dock = DockStyle.Fill, Height = 82, BackColor = Bg, Padding = new Padding(5) };
        var label = NewLabel(caption, 9); label.AutoSize = true; label.Location = new Point(5, 4);
        editor.Location = new Point(5, 28); editor.Width = 330; editor.Anchor = AnchorStyles.Left | AnchorStyles.Right | AnchorStyles.Top;
        box.Controls.Add(label); box.Controls.Add(editor); grid.Controls.Add(box, column, row);
    }

    private void WireEvents()
    {
        _systemEnabled.CheckedChanged += (_, _) => { if (!_loading) { _settings.SystemEnabled = _systemEnabled.Checked; SaveSoon(); } };
        _deafenEnabled.CheckedChanged += (_, _) => { if (!_loading) { _settings.DeafenEnabled = _deafenEnabled.Checked; SaveSoon(); } };
        foreach (var (mode, radio) in _muteModes) radio.CheckedChanged += (_, _) => { if (!_loading && radio.Checked) { _settings.MuteMode = mode; StyleModes(); SaveSoon(); } };
        foreach (var (mode, radio) in _deafenModes) radio.CheckedChanged += (_, _) => { if (!_loading && radio.Checked) { _settings.DeafenMode = mode; StyleModes(); SaveSoon(); } };
        foreach (var text in new[] { _muteHotkey, _deafenHotkey, _muteParameter, _toggleParameter, _deafenParameter, _vrchatIp }) text.TextChanged += (_, _) => { if (!_loading) SaveSoon(); };
        _oscMode.SelectedIndexChanged += (_, _) =>
        {
            if (_loading) return;
            _settings.OscConnectionMode = _oscMode.SelectedIndex == 1 ? OscConnectionMode.Manual : OscConnectionMode.OscQuery;
            UpdateOscModeControls();
            SaveSoon();
        };
        _sendPort.ValueChanged += (_, _) => { if (!_loading) SaveSoon(); };
        _receivePort.ValueChanged += (_, _) => { if (!_loading) SaveSoon(); };
        _logging.CheckedChanged += (_, _) => { if (!_loading) SaveSoon(); };
        _saveTimer.Tick += (_, _) => SaveNow();
        _refreshTimer.Tick += (_, _) => RefreshStatus();
        _recordFinishTimer.Tick += (_, _) => FinishRecording(true, "Keybind recorded and saved");
        _recordTimeoutTimer.Tick += (_, _) => FinishRecording(false, "Key recording timed out");
        _coordinator.Updated += CoordinatorUpdated;
        FormClosed += (_, _) => FinishRecording(false, "");
    }

    private void LoadSettingsIntoControls()
    {
        _systemEnabled.Checked = _settings.SystemEnabled;
        _deafenEnabled.Checked = _settings.DeafenEnabled;
        _muteModes[_settings.MuteMode].Checked = true;
        _deafenModes[_settings.DeafenMode].Checked = true;
        _muteHotkey.Text = _settings.MuteHotkey;
        _deafenHotkey.Text = _settings.DeafenHotkey;
        _muteParameter.Text = _settings.MuteParameter;
        _toggleParameter.Text = _settings.ToggleParameter;
        _deafenParameter.Text = _settings.DeafenParameter;
        _oscMode.SelectedIndex = _settings.OscConnectionMode == OscConnectionMode.Manual ? 1 : 0;
        _vrchatIp.Text = _settings.VrchatIp;
        _sendPort.Value = _settings.OscSendPort;
        _receivePort.Value = _settings.OscReceivePort;
        _logging.Checked = _settings.LoggingEnabled;
        UpdateOscModeControls();
        StyleModes();
    }

    private void SaveSoon()
    {
        _saveTimer.Stop();
        _saveTimer.Start();
    }

    private void SaveNow()
    {
        _saveTimer.Stop();
        _settings.MuteHotkey = _muteHotkey.Text.Trim();
        _settings.DeafenHotkey = _deafenHotkey.Text.Trim();
        _settings.MuteParameter = _muteParameter.Text.Trim();
        _settings.ToggleParameter = _toggleParameter.Text.Trim();
        _settings.DeafenParameter = _deafenParameter.Text.Trim();
        _settings.OscConnectionMode = _oscMode.SelectedIndex == 1 ? OscConnectionMode.Manual : OscConnectionMode.OscQuery;
        _settings.VrchatIp = _vrchatIp.Text.Trim();
        _settings.OscSendPort = (int)_sendPort.Value;
        _settings.OscReceivePort = (int)_receivePort.Value;
        _settings.LoggingEnabled = _logging.Checked;
        _coordinator.SettingsChanged();
    }

    private void StartRecording(TextBox target, Button button)
    {
        if (_recordTarget == target) { FinishRecording(false, "Key recording cancelled"); return; }
        FinishRecording(false, "");
        try
        {
            _recordTarget = target; _recordButton = button; _recordedKeys.Clear();
            _coordinator.Discord.CommandsSuspended = true;
            button.Text = "◉"; button.BackColor = Red; button.ForeColor = Color.White;
            _recorder = new GlobalKeyboardRecorder();
            _recorder.KeyPressed += RecorderKeyPressed;
            _recordTimeoutTimer.Start();
            _coordinator.SetStatus("Recording keybind — press a key (30 second timeout)");
        }
        catch (Exception ex) { FinishRecording(false, "Could not record keys: " + ex.Message); }
    }

    private void RecorderKeyPressed(Keys key)
    {
        if (IsDisposed) return;
        BeginInvoke(() =>
        {
            string name = NativeKeyboard.DisplayName(key);
            if (_recordTarget is null || _recordedKeys.Contains(name)) return;
            _recordedKeys.Add(name);
            if (!_recordFinishTimer.Enabled)
            {
                _recordFinishTimer.Start();
                _coordinator.SetStatus("First key recorded — adding keys for 5 seconds…");
            }
        });
    }

    private void FinishRecording(bool save, string message)
    {
        _recordFinishTimer.Stop(); _recordTimeoutTimer.Stop();
        if (_recorder is not null) { _recorder.KeyPressed -= RecorderKeyPressed; _recorder.Dispose(); _recorder = null; }
        if (_recordButton is not null) { _recordButton.Text = "●"; _recordButton.BackColor = Panel2; _recordButton.ForeColor = Red; }
        var target = _recordTarget;
        _recordTarget = null; _recordButton = null;
        _coordinator.Discord.CommandsSuspended = false;
        if (save && target is not null && _recordedKeys.Count > 0) { target.Text = string.Join('+', _recordedKeys); SaveNow(); }
        _recordedKeys.Clear();
        if (!string.IsNullOrEmpty(message)) _coordinator.SetStatus(message);
    }

    private void CoordinatorUpdated()
    {
        if (!IsDisposed && IsHandleCreated) BeginInvoke(RefreshStatus);
    }

    private void RefreshStatus()
    {
        if (IsDisposed) return;
        SetState(_discordState, _coordinator.Discord.Muted);
        _discordInfo.Text = _coordinator.Discord.Ready ? "Accessibility reader ready" : "Searching Discord UI…";
        _discordInfo.ForeColor = _coordinator.Discord.Ready ? Cyan : MutedColor;
        bool? deaf = _coordinator.Discord.Deafened;
        _discordDeafen.Text = deaf switch { true => "DEAFENED — mic sync paused", false => "Deafen: off", _ => "Deafen: waiting" };
        _discordDeafen.ForeColor = deaf == true ? Red : MutedColor;
        SetState(_vrcState, _coordinator.Vrchat.Muted);
        _vrcConnection.Text = _coordinator.Vrchat.ConnectionDescription;
        _vrcConnection.ForeColor = _coordinator.Vrchat.Connected ? (_coordinator.Vrchat.TargetReady ? Cyan : MutedColor) : Red;
        _vrcInfo.Text = _coordinator.Vrchat.MuteFound ? $"{_settings.MuteParameter} detected" : $"Waiting for {_settings.MuteParameter}";
        _vrcInfo.ForeColor = _coordinator.Vrchat.MuteFound ? Cyan : MutedColor;
        _vrcDeafen.Text = !_settings.DeafenEnabled ? "Deafen parameter bridge: off" : _coordinator.Vrchat.DeafenFound ? $"{_settings.DeafenParameter}: {_coordinator.Vrchat.Deafened}" : $"Waiting for {_settings.DeafenParameter}";
        _vrcDeafen.ForeColor = _coordinator.Vrchat.DeafenFound ? Cyan : MutedColor;
        _action.Text = _coordinator.LastAction;
        if (_systemEnabled.Checked != _settings.SystemEnabled) { _loading = true; _systemEnabled.Checked = _settings.SystemEnabled; _loading = false; }
    }

    private static void SetState(Label label, bool? state)
    {
        label.Text = state switch { true => "MUTED", false => "LIVE", _ => "WAITING" };
        label.ForeColor = state switch { true => Red, false => Cyan, _ => MutedColor };
    }

    private void StyleModes()
    {
        Style(_muteModes, _settings.MuteMode, Blue, Color.White);
        Style(_deafenModes, _settings.DeafenMode, Cyan, Bg);
    }

    private void UpdateOscModeControls()
    {
        bool manual = _oscMode.SelectedIndex == 1;
        _vrchatIp.Enabled = manual;
        _sendPort.Enabled = manual;
        _receivePort.Enabled = manual;
    }

    private static void Style(Dictionary<SyncMode, RadioButton> controls, SyncMode selected, Color active, Color activeText)
    {
        foreach (var (mode, radio) in controls) { radio.BackColor = mode == selected ? active : Panel2; radio.ForeColor = mode == selected ? activeText : MutedColor; }
    }

    private static Panel NewPanel() => new() { Dock = DockStyle.Fill, BackColor = Panel, Margin = new Padding(5) };
    private static TabPage NewPage(string text) => new(text) { BackColor = Bg, ForeColor = TextColor };
    private static Label NewLabel(string text, float size, bool bold = false) => new() { Text = text, ForeColor = bold ? TextColor : MutedColor, BackColor = Color.Transparent, Font = new Font("Segoe UI", size, bold ? FontStyle.Bold : FontStyle.Regular) };
    private static CheckBox NewCheck(string text) => new() { Text = text, ForeColor = TextColor, BackColor = Color.Transparent, FlatStyle = FlatStyle.Flat };
    private static TextBox NewTextBox() => new() { BackColor = Panel2, ForeColor = TextColor, BorderStyle = BorderStyle.FixedSingle, Font = new Font("Segoe UI", 10) };
    private static NumericUpDown NewPort() => new() { Minimum = 1, Maximum = 65535, BackColor = Panel2, ForeColor = TextColor, BorderStyle = BorderStyle.FixedSingle, Width = 160 };
}
