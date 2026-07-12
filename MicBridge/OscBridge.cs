using System.Buffers.Binary;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using VRC.OSCQuery;
using OscQueryExtensions = VRC.OSCQuery.Extensions;

namespace MicBridge;

public sealed class OscBridge : IDisposable
{
    private readonly AppSettings _settings;
    private readonly AppLog _log;
    private readonly object _gate = new();
    private CancellationTokenSource? _stop;
    private UdpClient? _listener;
    private UdpClient? _sender;
    private OSCQueryService? _oscQuery;
    private Task? _receiveTask;
    private Task? _discoveryTask;
    private IPAddress? _sendAddress;
    private int _sendPort;
    private int _receivePort;
    private int _queryPort;
    private int _generation;
    private bool? _mute, _deafen, _toggle;
    private bool? _expectedMute, _expectedDeafen;
    private DateTime _expectedMuteUntil, _expectedDeafenUntil;

    public event Action<BridgeSignal>? Signal;
    public event Action? StatusChanged;
    public bool Connected { get; private set; }
    public bool TargetReady { get { lock (_gate) return _sendAddress is not null && _sendPort > 0; } }
    public bool MuteFound { get; private set; }
    public bool DeafenFound { get; private set; }
    public bool? Muted { get { lock (_gate) return _mute; } }
    public bool? Deafened { get { lock (_gate) return _deafen; } }
    public string ConnectionDescription
    {
        get
        {
            lock (_gate)
            {
                if (!Connected) return _settings.OscConnectionMode == OscConnectionMode.OscQuery
                    ? "OSCQuery could not start" : "Manual OSC could not start";
                if (_settings.OscConnectionMode == OscConnectionMode.Manual)
                    return $"Manual: {_settings.VrchatIp}:{_sendPort}  •  listen {_receivePort}";
                return _sendAddress is null
                    ? $"OSCQuery: listen {_receivePort}  •  finding VRChat…"
                    : $"OSCQuery: {_sendAddress}:{_sendPort}  •  listen {_receivePort}";
            }
        }
    }

    public OscBridge(AppSettings settings, AppLog log) { _settings = settings; _log = log; }

    public void Start() => Restart();

    public void Restart()
    {
        StopListener();
        int generation = ++_generation;
        try
        {
            _stop = new CancellationTokenSource();
            _sender = new UdpClient();

            if (_settings.OscConnectionMode == OscConnectionMode.Manual)
                StartManual();
            else
                StartOscQuery(generation);

            Connected = true;
            _receiveTask = Task.Run(() => ReceiveLoop(_stop.Token));
        }
        catch (Exception ex)
        {
            _log.Write("VRChat OSC failed: " + ex.Message);
            StopListener();
        }
        StatusChanged?.Invoke();
    }

    private void StartManual()
    {
        _receivePort = _settings.OscReceivePort;
        _listener = new UdpClient(new IPEndPoint(IPAddress.Loopback, _receivePort));
        if (!IPAddress.TryParse(_settings.VrchatIp, out _sendAddress))
            _sendAddress = Dns.GetHostAddresses(_settings.VrchatIp).First(address => address.AddressFamily == AddressFamily.InterNetwork);
        _sendPort = _settings.OscSendPort;
        _queryPort = 0;
        _log.Write($"Manual VRChat OSC ready: {_sendAddress}:{_sendPort}, receive {_receivePort}");
    }

    private void StartOscQuery(int generation)
    {
        _listener = new UdpClient(new IPEndPoint(IPAddress.Loopback, 0));
        _receivePort = ((IPEndPoint)_listener.Client.LocalEndPoint!).Port;
        _queryPort = OscQueryExtensions.GetAvailableTcpPort();
        lock (_gate) { _sendAddress = null; _sendPort = 0; }

        var discovery = new MeaModDiscovery();
        _oscQuery = new OSCQueryServiceBuilder()
            .WithServiceName("MicBridge")
            .WithTcpPort(_queryPort)
            .WithUdpPort(_receivePort)
            .WithHostIP(IPAddress.Loopback)
            .StartHttpServer()
            .WithDiscovery(discovery)
            .AdvertiseOSCQuery()
            .AdvertiseOSC()
            .Build();

        AddQueryEndpoint(_settings.MuteParameter);
        AddQueryEndpoint(_settings.ToggleParameter);
        AddQueryEndpoint(_settings.DeafenParameter);
        _oscQuery.OnOscServiceAdded += profile => AcceptOscService(profile, generation);
        _oscQuery.OnOscQueryServiceAdded += profile => _ = AcceptOscQueryService(profile, generation);

        foreach (var profile in _oscQuery.GetOSCServices()) AcceptOscService(profile, generation);
        foreach (var profile in _oscQuery.GetOSCQueryServices()) _ = AcceptOscQueryService(profile, generation);
        _oscQuery.RefreshServices();
        _discoveryTask = Task.Run(() => DiscoveryLoop(_stop!.Token, generation));
        _log.Write($"OSCQuery advertised: HTTP {_queryPort}, OSC receive {_receivePort}; discovering VRChat");
    }

    private void AddQueryEndpoint(string name)
    {
        if (!string.IsNullOrWhiteSpace(name))
            _oscQuery!.AddEndpoint<bool>($"/avatar/parameters/{name}", Attributes.AccessValues.WriteOnly);
    }

    private async Task DiscoveryLoop(CancellationToken token, int generation)
    {
        while (!token.IsCancellationRequested && generation == _generation)
        {
            try
            {
                await Task.Delay(TimeSpan.FromSeconds(5), token);
                _oscQuery?.RefreshServices();
            }
            catch (OperationCanceledException) { break; }
            catch (Exception ex) { _log.Write("OSCQuery refresh failed: " + ex.Message); }
        }
    }

    private static bool IsVrchat(OSCQueryServiceProfile profile) =>
        profile.name?.Contains("VRChat", StringComparison.OrdinalIgnoreCase) == true;

    private void AcceptOscService(OSCQueryServiceProfile profile, int generation)
    {
        if (!IsVrchat(profile) || generation != _generation || profile.port <= 0) return;
        SetQueryTarget(profile.address, profile.port, $"OSC service {profile.name}", generation);
    }

    private async Task AcceptOscQueryService(OSCQueryServiceProfile profile, int generation)
    {
        if (!IsVrchat(profile) || generation != _generation) return;
        try
        {
            HostInfo info = await OscQueryExtensions.GetHostInfo(profile.address, profile.port);
            IPAddress address = IPAddress.TryParse(info.oscIP, out var advertised) ? advertised : profile.address;
            SetQueryTarget(address, info.oscPort, $"OSCQuery service {profile.name}", generation);
        }
        catch (Exception ex) { _log.Write($"Could not read VRChat OSCQuery HOST_INFO: {ex.Message}"); }
    }

    private void SetQueryTarget(IPAddress address, int port, string source, int generation)
    {
        if (generation != _generation || port <= 0) return;
        bool changed;
        lock (_gate)
        {
            changed = !_sendAddress?.Equals(address) ?? true;
            changed |= _sendPort != port;
            _sendAddress = address;
            _sendPort = port;
        }
        if (changed)
        {
            _log.Write($"VRChat discovered from {source}: {address}:{port}");
            StatusChanged?.Invoke();
        }
    }

    private async Task ReceiveLoop(CancellationToken token)
    {
        while (!token.IsCancellationRequested && _listener is not null)
        {
            try
            {
                var packet = await _listener.ReceiveAsync(token);
                if (TryParse(packet.Buffer, out string address, out bool value)) Handle(address, value);
            }
            catch (OperationCanceledException) { break; }
            catch (ObjectDisposedException) { break; }
            catch (Exception ex) { _log.Write("OSC receive failed: " + ex.Message); }
        }
    }

    private void Handle(string address, bool value)
    {
        string name = address[(address.LastIndexOf('/') + 1)..];
        if (name.Equals(_settings.MuteParameter, StringComparison.OrdinalIgnoreCase))
            AcceptMute(value);
        else if (!string.IsNullOrWhiteSpace(_settings.DeafenParameter)
                 && name.Equals(_settings.DeafenParameter, StringComparison.OrdinalIgnoreCase))
            AcceptDeafen(value);
        else if (!string.IsNullOrWhiteSpace(_settings.ToggleParameter)
                 && name.Equals(_settings.ToggleParameter, StringComparison.OrdinalIgnoreCase))
        {
            bool initial;
            lock (_gate) { initial = !_toggle.HasValue; if (_toggle == value) return; _toggle = value; }
            Signal?.Invoke(new(SignalKind.VrchatToggle, value, initial));
        }
    }

    private void AcceptMute(bool value)
    {
        BridgeSignal signal;
        lock (_gate)
        {
            bool initial = !MuteFound;
            bool changed = initial || _mute != value;
            MuteFound = true;
            _mute = value;
            bool expected = _expectedMute == value && DateTime.UtcNow <= _expectedMuteUntil;
            _expectedMute = null;
            if (!changed && !expected) return;
            signal = new(SignalKind.VrchatMute, value, initial, expected);
        }
        StatusChanged?.Invoke();
        Signal?.Invoke(signal);
    }

    private void AcceptDeafen(bool value)
    {
        BridgeSignal signal;
        lock (_gate)
        {
            bool initial = !DeafenFound;
            bool changed = initial || _deafen != value;
            DeafenFound = true;
            _deafen = value;
            bool expected = _expectedDeafen == value && DateTime.UtcNow <= _expectedDeafenUntil;
            _expectedDeafen = null;
            if (!changed && !expected) return;
            signal = new(SignalKind.VrchatDeafen, value, initial, expected);
        }
        StatusChanged?.Invoke();
        Signal?.Invoke(signal);
    }

    public bool SetMute(bool target)
    {
        lock (_gate)
        {
            if (!Connected || !MuteFound || _mute == target) return false;
            // MuteSelf is an outgoing state value, not a writable mic command.
            // VRChat's reliable mic control is an /input/Voice press and release.
            if (!SendVoiceToggle()) return false;
            _mute = target;
            _expectedMute = target;
            _expectedMuteUntil = DateTime.UtcNow.AddSeconds(1.5);
        }
        StatusChanged?.Invoke();
        return true;
    }

    public bool SetDeafen(bool target)
    {
        lock (_gate)
        {
            if (!Connected || !DeafenFound || _deafen == target || string.IsNullOrWhiteSpace(_settings.DeafenParameter)) return false;
            if (!SendParameter(_settings.DeafenParameter, target)) return false;
            _deafen = target;
            _expectedDeafen = target;
            _expectedDeafenUntil = DateTime.UtcNow.AddSeconds(1.5);
        }
        StatusChanged?.Invoke();
        return true;
    }

    private bool SendParameter(string name, bool value)
    {
        try
        {
            if (_sender is null || _sendAddress is null || _sendPort <= 0)
            {
                _log.Write("OSC send skipped: waiting for VRChat OSCQuery discovery");
                return false;
            }
            byte[] data = Build($"/avatar/parameters/{name}", value);
            _sender.Send(data, data.Length, new IPEndPoint(_sendAddress, _sendPort));
            _log.Write($"OSC set {name} -> {value}");
            return true;
        }
        catch (Exception ex) { _log.Write("OSC send failed: " + ex.Message); return false; }
    }

    private bool SendVoiceToggle()
    {
        try
        {
            if (_sender is null || _sendAddress is null || _sendPort <= 0)
            {
                _log.Write("VRChat voice toggle skipped: waiting for VRChat OSCQuery discovery");
                return false;
            }

            var endpoint = new IPEndPoint(_sendAddress, _sendPort);
            byte[] pressed = BuildInt("/input/Voice", 1);
            _sender.Send(pressed, pressed.Length, endpoint);
            Thread.Sleep(Math.Clamp(_settings.VrchatVoicePressMs, 20, 1000));
            byte[] released = BuildInt("/input/Voice", 0);
            _sender.Send(released, released.Length, endpoint);
            _log.Write($"VRChat /input/Voice pulse sent ({_settings.VrchatVoicePressMs}ms)");
            return true;
        }
        catch (Exception ex)
        {
            _log.Write("VRChat /input/Voice toggle failed: " + ex.Message);
            return false;
        }
    }

    internal static byte[] Build(string address, bool value)
    {
        using var stream = new MemoryStream();
        WriteString(stream, address);
        WriteString(stream, ",f");
        Span<byte> bytes = stackalloc byte[4];
        BinaryPrimitives.WriteInt32BigEndian(bytes, BitConverter.SingleToInt32Bits(value ? 1f : 0f));
        stream.Write(bytes);
        return stream.ToArray();
    }

    internal static byte[] BuildInt(string address, int value)
    {
        using var stream = new MemoryStream();
        WriteString(stream, address);
        WriteString(stream, ",i");
        Span<byte> bytes = stackalloc byte[4];
        BinaryPrimitives.WriteInt32BigEndian(bytes, value);
        stream.Write(bytes);
        return stream.ToArray();
    }

    internal static bool TryParse(byte[] data, out string address, out bool value)
    {
        address = ""; value = false;
        int offset = 0;
        if (!ReadString(data, ref offset, out address) || !ReadString(data, ref offset, out string types)) return false;
        if (types.Contains('T')) { value = true; return true; }
        if (types.Contains('F')) { value = false; return true; }
        if (offset + 4 > data.Length) return false;
        int raw = BinaryPrimitives.ReadInt32BigEndian(data.AsSpan(offset, 4));
        if (types.Contains('f')) value = BitConverter.Int32BitsToSingle(raw) != 0f;
        else if (types.Contains('i')) value = raw != 0;
        else return false;
        return true;
    }

    private static void WriteString(Stream stream, string value)
    {
        byte[] bytes = Encoding.UTF8.GetBytes(value);
        stream.Write(bytes);
        stream.WriteByte(0);
        while (stream.Position % 4 != 0) stream.WriteByte(0);
    }

    private static bool ReadString(byte[] data, ref int offset, out string value)
    {
        value = "";
        if (offset >= data.Length) return false;
        int end = Array.IndexOf(data, (byte)0, offset);
        if (end < 0) return false;
        value = Encoding.UTF8.GetString(data, offset, end - offset);
        offset = (end + 4) & ~3;
        return true;
    }

    private void StopListener()
    {
        ++_generation;
        _stop?.Cancel();
        _listener?.Dispose();
        _oscQuery?.Dispose();
        try { _receiveTask?.Wait(500); } catch { }
        try { _discoveryTask?.Wait(500); } catch { }
        _sender?.Dispose();
        _stop?.Dispose();
        _stop = null; _listener = null; _sender = null; _oscQuery = null;
        _receiveTask = null; _discoveryTask = null;
        Connected = false;
        MuteFound = DeafenFound = false;
        lock (_gate)
        {
            _mute = _deafen = _toggle = null;
            _sendAddress = null; _sendPort = _receivePort = _queryPort = 0;
        }
    }

    public void Dispose() => StopListener();
}
