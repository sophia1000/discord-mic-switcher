using System.Drawing;

namespace MicBridge;

public sealed class MainForm : Form
{
    private static readonly Color Bg = Color.FromArgb(8, 12, 18);
    private static readonly Color Panel = Color.FromArgb(17, 24, 36);
    private static readonly Color Panel2 = Color.FromArgb(27, 38, 55);
    private static readonly Color Border = Color.FromArgb(43, 57, 78);
    private static readonly Color TextColor = Color.FromArgb(248, 250, 252);
    private static readonly Color MutedColor = Color.FromArgb(157, 171, 192);
    private static readonly Color Blue = Color.FromArgb(99, 102, 241);
    private static readonly Color Cyan = Color.FromArgb(45, 212, 191);
    private static readonly Color Green = Color.FromArgb(34, 197, 94);
    private static readonly Color Red = Color.FromArgb(251, 113, 133);

    private readonly SyncCoordinator _coordinator;
    private readonly AppSettings _settings;
    private readonly SettingsStore _store;
    private readonly System.Windows.Forms.Timer _saveTimer = new() { Interval = 500 };
    private readonly System.Windows.Forms.Timer _refreshTimer = new() { Interval = 150 };
    private bool _loading = true;

    private readonly Label _discordState = NewLabel("WAITING", 22, true);
    private readonly Label _discordInfo = NewLabel("Searching Discord UI…", 9);
    private readonly Label _discordDeafen = NewLabel("Deafen: waiting", 9, true);
    private readonly Label _vrcState = NewLabel("WAITING", 22, true);
    private readonly Label _vrcConnection = NewLabel("Starting OSCQuery…", 8);
    private readonly Label _vrcInfo = NewLabel("Waiting for MuteSelf", 9);
    private readonly Label _vrcDeafen = NewLabel("Deafen parameter: off", 9, true);
    private readonly Label _action = NewLabel("Starting…", 9);
    private readonly CheckBox _systemEnabled = NewCheck("");
    private readonly CheckBox _deafenEnabled = NewCheck("");
    private readonly Dictionary<SyncMode, RadioButton> _muteModes = [];
    private readonly Dictionary<SyncMode, RadioButton> _deafenModes = [];
    private readonly TextBox _muteParameter = NewTextBox();
    private readonly TextBox _toggleParameter = NewTextBox();
    private readonly TextBox _deafenParameter = NewTextBox();
    private readonly ComboBox _oscMode = new() { DropDownStyle = ComboBoxStyle.DropDownList, BackColor = Panel2, ForeColor = TextColor, FlatStyle = FlatStyle.Flat };
    private readonly TextBox _vrchatIp = NewTextBox();
    private readonly NumericUpDown _sendPort = NewPort();
    private readonly NumericUpDown _receivePort = NewPort();
    private readonly CheckBox _logging = NewCheck("Write diagnostic log");

    public MainForm(SyncCoordinator coordinator, AppSettings settings, SettingsStore store)
    {
        _coordinator = coordinator;
        _settings = settings;
        _store = store;
        Text = "Mic Bridge";
        ClientSize = new Size(900, 780);
        MinimumSize = new Size(800, 720);
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

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        NativeWindows.EnableDarkTitleBar(Handle);
    }

    private Control BuildLayout()
    {
        var root = new TableLayoutPanel { Dock = DockStyle.Fill, BackColor = Bg, Padding = new Padding(26, 20, 26, 16), RowCount = 4 };
        root.RowStyles.Add(new RowStyle(SizeType.Absolute, 66));
        root.RowStyles.Add(new RowStyle(SizeType.Absolute, 48));
        root.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        root.RowStyles.Add(new RowStyle(SizeType.Absolute, 40));

        var header = new Panel { Dock = DockStyle.Fill, BackColor = Bg };
        var title = NewLabel("MIC BRIDGE", 24, true); title.AutoSize = true; title.Location = new Point(0, 3);
        var sub = NewLabel("Discord  ↔  VRChat", 10); sub.AutoSize = true; sub.Location = new Point(205, 18);
        sub.ForeColor = Cyan;
        header.Controls.AddRange([title, sub]);
        root.Controls.Add(header, 0, 0);

        var navigation = new FlowLayoutPanel { Dock = DockStyle.Fill, BackColor = Bg, FlowDirection = FlowDirection.LeftToRight, Padding = new Padding(0), Margin = new Padding(0) };
        var bridgeButton = NavigationButton("Bridge");
        var settingsButton = NavigationButton("Settings");
        navigation.Controls.AddRange([bridgeButton, settingsButton]);
        root.Controls.Add(navigation, 0, 1);

        var content = new Panel { Dock = DockStyle.Fill, BackColor = Bg, Margin = new Padding(0) };
        var bridgePage = BuildBridgePage();
        var settingsPage = BuildSettingsPage();
        settingsPage.Visible = false;
        content.Controls.Add(settingsPage);
        content.Controls.Add(bridgePage);
        SelectNavigation(bridgeButton, settingsButton);
        bridgeButton.Click += (_, _) => { bridgePage.Visible = true; settingsPage.Visible = false; bridgePage.BringToFront(); SelectNavigation(bridgeButton, settingsButton); };
        settingsButton.Click += (_, _) => { bridgePage.Visible = false; settingsPage.Visible = true; settingsPage.BringToFront(); SelectNavigation(settingsButton, bridgeButton); };
        root.Controls.Add(content, 0, 2);

        var footer = new Panel { Dock = DockStyle.Fill, BackColor = Bg, Padding = new Padding(2, 8, 0, 0) };
        footer.Paint += (_, e) => { using var pen = new Pen(Border); e.Graphics.DrawLine(pen, 0, 0, footer.Width, 0); };
        _action.Dock = DockStyle.Fill; _action.TextAlign = ContentAlignment.MiddleLeft;
        footer.Controls.Add(_action);
        root.Controls.Add(footer, 0, 3);
        return root;
    }

    private Control BuildBridgePage()
    {
        var page = NewPage();
        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, BackColor = Bg, Padding = new Padding(0, 10, 0, 0), RowCount = 4 };
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 178));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 198));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 198));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));

        var status = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, BackColor = Bg };
        status.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50));
        status.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50));
        status.Controls.Add(StatusCard("DISCORD", _discordState, _discordInfo, _discordDeafen), 0, 0);
        status.Controls.Add(StatusCard("VRCHAT", _vrcState, _vrcConnection, _vrcInfo, _vrcDeafen), 1, 0);
        layout.Controls.Add(status, 0, 0);
        var muteCard = DirectionCard("MUTE DIRECTION", _muteModes, _systemEnabled);
        layout.Controls.Add(muteCard, 0, 1);

        var deafenCard = DirectionCard("DEAFEN PARAMETER DIRECTION", _deafenModes, _deafenEnabled);
        layout.Controls.Add(deafenCard, 0, 2);

        var hint = NewLabel("Mute states stay opposite. Deafen states match the custom VRChat boolean directly.", 9);
        hint.Dock = DockStyle.Top; hint.Padding = new Padding(4, 8, 0, 0);
        layout.Controls.Add(hint, 0, 3);
        page.Controls.Add(layout);
        return page;
    }

    private Control BuildSettingsPage()
    {
        var page = NewPage();
        page.AutoScroll = true;
        var grid = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, BackColor = Panel, Padding = new Padding(22), ColumnCount = 2, RowCount = 6, Margin = new Padding(0, 10, 0, 0) };
        grid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50));
        grid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 50));

        var heading = NewLabel("CONNECTION & PARAMETERS", 10, true); heading.AutoSize = true; heading.Padding = new Padding(5, 0, 0, 8);
        grid.Controls.Add(heading, 0, 0); grid.SetColumnSpan(heading, 2);
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
        var frame = new BorderedPanel(Border) { Dock = DockStyle.Top, Height = 505, BackColor = Panel, Margin = new Padding(0, 10, 0, 0) };
        frame.Controls.Add(grid);
        page.Controls.Add(frame);
        return page;
    }

    private Panel StatusCard(string title, params Label[] labels)
    {
        var panel = NewPanel(); panel.Margin = new Padding(6); panel.Padding = new Padding(20);
        var heading = NewLabel(title, 9, true); heading.AutoSize = true; heading.Location = new Point(18, 12);
        panel.Controls.Add(heading);
        int y = 35;
        foreach (var label in labels) { label.AutoSize = true; label.Location = new Point(18, y); panel.Controls.Add(label); y += label.Font.Size > 15 ? 40 : 23; }
        return panel;
    }

    private Panel DirectionCard(string title, Dictionary<SyncMode, RadioButton> destination, CheckBox enabled)
    {
        var panel = NewPanel(); panel.Margin = new Padding(6); panel.Padding = new Padding(18, 14, 18, 18);
        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, BackColor = Panel, ColumnCount = 3, RowCount = 3, Margin = new Padding(0) };
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 33.333f));
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 33.334f));
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 33.333f));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 28));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 42));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        var heading = NewLabel(title, 9, true); heading.AutoSize = true; heading.Dock = DockStyle.Fill; layout.Controls.Add(heading, 0, 0); layout.SetColumnSpan(heading, 3);
        ConfigureEnableButton(enabled); enabled.Dock = DockStyle.Left; layout.Controls.Add(enabled, 0, 1); layout.SetColumnSpan(enabled, 3);
        var items = new[] { (SyncMode.VrchatMaster, "VRChat master"), (SyncMode.Dynamic, "Dynamic"), (SyncMode.DiscordMaster, "Discord master") };
        int column = 0;
        foreach (var (mode, text) in items)
        {
            var radio = new RadioButton
            {
                Appearance = Appearance.Button, Text = text, TextAlign = ContentAlignment.MiddleCenter,
                FlatStyle = FlatStyle.Flat, BackColor = Panel2, ForeColor = MutedColor,
                Dock = DockStyle.Fill, Margin = new Padding(column == 0 ? 0 : 6, 8, column == 2 ? 0 : 6, 0), Tag = mode, Cursor = Cursors.Hand
            };
            radio.FlatAppearance.BorderColor = Border;
            radio.FlatAppearance.BorderSize = 1;
            radio.FlatAppearance.MouseOverBackColor = Color.FromArgb(36, 49, 69);
            destination[mode] = radio; layout.Controls.Add(radio, column++, 2);
        }
        panel.Controls.Add(layout);
        return panel;
    }

    private static void AddSetting(TableLayoutPanel grid, int column, int row, string caption, Control editor)
    {
        var box = new Panel { Dock = DockStyle.Fill, Height = 82, BackColor = Panel, Padding = new Padding(5) };
        var label = NewLabel(caption, 9); label.AutoSize = true; label.Location = new Point(5, 4);
        editor.Location = new Point(5, 28); editor.Width = 330; editor.Anchor = AnchorStyles.Left | AnchorStyles.Right | AnchorStyles.Top;
        box.Controls.Add(label); box.Controls.Add(editor); grid.Controls.Add(box, column, row);
    }

    private void WireEvents()
    {
        _systemEnabled.CheckedChanged += (_, _) => { StyleEnableButton(_systemEnabled); if (!_loading) { _settings.SystemEnabled = _systemEnabled.Checked; SaveSoon(); } };
        _deafenEnabled.CheckedChanged += (_, _) => { StyleEnableButton(_deafenEnabled); if (!_loading) { _settings.DeafenEnabled = _deafenEnabled.Checked; SaveSoon(); } };
        foreach (var (mode, radio) in _muteModes) radio.CheckedChanged += (_, _) => { if (!_loading && radio.Checked) { _settings.MuteMode = mode; StyleModes(); SaveSoon(); } };
        foreach (var (mode, radio) in _deafenModes) radio.CheckedChanged += (_, _) => { if (!_loading && radio.Checked) { _settings.DeafenMode = mode; StyleModes(); SaveSoon(); } };
        foreach (var text in new[] { _muteParameter, _toggleParameter, _deafenParameter, _vrchatIp }) text.TextChanged += (_, _) => { if (!_loading) SaveSoon(); };
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
        _coordinator.Updated += CoordinatorUpdated;
    }

    private void LoadSettingsIntoControls()
    {
        _systemEnabled.Checked = _settings.SystemEnabled;
        _deafenEnabled.Checked = _settings.DeafenEnabled;
        _muteModes[_settings.MuteMode].Checked = true;
        _deafenModes[_settings.DeafenMode].Checked = true;
        _muteParameter.Text = _settings.MuteParameter;
        _toggleParameter.Text = _settings.ToggleParameter;
        _deafenParameter.Text = _settings.DeafenParameter;
        _oscMode.SelectedIndex = _settings.OscConnectionMode == OscConnectionMode.Manual ? 1 : 0;
        _vrchatIp.Text = _settings.VrchatIp;
        _sendPort.Value = _settings.OscSendPort;
        _receivePort.Value = _settings.OscReceivePort;
        _logging.Checked = _settings.LoggingEnabled;
        UpdateOscModeControls();
        StyleEnableButton(_systemEnabled);
        StyleEnableButton(_deafenEnabled);
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
        _vrcDeafen.Text = !_settings.DeafenEnabled ? "" : _coordinator.Vrchat.DeafenFound ? $"{_settings.DeafenParameter}: {_coordinator.Vrchat.Deafened}" : $"Waiting for {_settings.DeafenParameter}";
        _vrcDeafen.ForeColor = _coordinator.Vrchat.DeafenFound ? Cyan : MutedColor;
        _action.Text = _coordinator.LastAction;
        if (_systemEnabled.Checked != _settings.SystemEnabled) { _loading = true; _systemEnabled.Checked = _settings.SystemEnabled; _loading = false; StyleEnableButton(_systemEnabled); }
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

    private static void ConfigureEnableButton(CheckBox control)
    {
        control.Appearance = Appearance.Button;
        control.AutoSize = false;
        control.Size = new Size(160, 34);
        control.Location = new Point(18, 42);
        control.TextAlign = ContentAlignment.MiddleCenter;
        control.Cursor = Cursors.Hand;
        control.FlatAppearance.BorderSize = 0;
        control.FlatAppearance.MouseOverBackColor = Panel2;
    }

    private static void StyleEnableButton(CheckBox control)
    {
        control.Text = control.Checked ? "Enabled" : "Disabled";
        control.BackColor = control.Checked ? Green : Red;
        control.ForeColor = Color.White;
    }

    private static void Style(Dictionary<SyncMode, RadioButton> controls, SyncMode selected, Color active, Color activeText)
    {
        foreach (var (mode, radio) in controls) { radio.BackColor = mode == selected ? active : Panel2; radio.ForeColor = mode == selected ? activeText : MutedColor; }
    }

    private static Button NavigationButton(string text)
    {
        var button = new Button { Text = text, Size = new Size(124, 40), Margin = new Padding(0, 0, 8, 0), FlatStyle = FlatStyle.Flat, BackColor = Bg, ForeColor = MutedColor, Cursor = Cursors.Hand };
        button.FlatAppearance.BorderSize = 0;
        button.FlatAppearance.MouseOverBackColor = Panel2;
        return button;
    }

    private static void SelectNavigation(Button selected, Button other)
    {
        selected.BackColor = Panel2; selected.ForeColor = TextColor;
        other.BackColor = Bg; other.ForeColor = MutedColor;
    }

    private static Panel NewPanel() => new BorderedPanel(Border) { Dock = DockStyle.Fill, BackColor = Panel, Margin = new Padding(5) };
    private static Panel NewPage() => new() { Dock = DockStyle.Fill, BackColor = Bg, ForeColor = TextColor };
    private static Label NewLabel(string text, float size, bool bold = false) => new() { Text = text, ForeColor = bold ? TextColor : MutedColor, BackColor = Color.Transparent, Font = new Font("Segoe UI", size, bold ? FontStyle.Bold : FontStyle.Regular) };
    private static CheckBox NewCheck(string text) => new() { Text = text, ForeColor = TextColor, BackColor = Color.Transparent, FlatStyle = FlatStyle.Flat };
    private static TextBox NewTextBox() => new() { BackColor = Panel2, ForeColor = TextColor, BorderStyle = BorderStyle.FixedSingle, Font = new Font("Segoe UI", 10), Height = 30 };
    private static NumericUpDown NewPort() => new() { Minimum = 1, Maximum = 65535, BackColor = Panel2, ForeColor = TextColor, BorderStyle = BorderStyle.FixedSingle, Width = 160 };
}

internal sealed class BorderedPanel(Color borderColor) : Panel
{
    protected override void OnPaint(PaintEventArgs e)
    {
        base.OnPaint(e);
        using var pen = new Pen(borderColor);
        e.Graphics.DrawRectangle(pen, 0, 0, Width - 1, Height - 1);
    }
}
