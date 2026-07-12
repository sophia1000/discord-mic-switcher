# Sophia's Mic Bridge — Native Windows Edition

Sophia's Mic Bridge is a native C# Windows application. It does not require Python,
the .NET runtime, or separate dependency files.

For a normal Windows installation, download and run:

`SophiasMicBridgeSetup.exe`

The installer adds Sophia's Mic Bridge to Windows Search and the Start Menu,
registers it in Installed Apps for clean uninstallation, and offers optional
desktop and start-with-Windows shortcuts.

For portable use, run the application with `launch.bat`, or directly open:

`dist\MicBridge\MicBridge.exe`

## Discord integration

Discord state is read through native Windows UI Automation UIA3—the same
accessibility information exposed to screen readers. No Discord token or
privileged Discord API is used.

Discord is controlled through the same Windows accessibility toggle controls
used to read its state. Commands are verified against the resulting Discord
state and retried when Discord is temporarily rebuilding its interface.

## Bridges

Mute states stay opposite between Discord and VRChat. Deafen states match the
custom VRChat boolean directly (`true` means deafened).

Mute and deafen each have independent direction modes:

- **VRChat master:** VRChat always decides.
- **Dynamic:** waits for a real change on either side; the latest change wins.
- **Discord master:** Discord always decides.

Mic synchronization pauses while Discord is deafened so Discord's automatic
mute side effect cannot cause a synchronization loop.

## Settings

Every setting saves automatically. Native settings and logs are stored in:

`%LOCALAPPDATA%\MicBridge`

On first launch, the C# application imports the old `mic_sync_config.json` when
it is available.

VRChat connectivity defaults to **OSCQuery (automatic)**. Mic Bridge advertises
its parameter endpoints and a free receive port, then discovers VRChat's OSC
destination automatically. Turn OSC on in VRChat; no port setup is normally
needed.

Choose **Manual IP and ports** to use the legacy fixed configuration instead.
The default manual destination is `127.0.0.1:9000`, with Mic Bridge listening
on the saved receive port.

## Building the installer

Install the .NET 10 SDK and Inno Setup 6, then run `build-installer.bat`. It
builds the application and produces the single-file installer at:

`dist\installer\SophiasMicBridgeSetup.exe`

If Inno Setup is missing, install it with:

`winget install --id JRSoftware.InnoSetup -e`

## Rebuilding the portable EXE

Install the .NET 10 SDK and run `build-exe.bat`. The script publishes a
self-contained, single-file Windows x64 executable to:

`dist\MicBridge\MicBridge.exe`

The source project is in `MicBridge\`. The previous Python implementation is
left in the repository only as a reference and is no longer launched.

## Third-party components

The native accessibility reader uses FlaUI UIA3 5.0.0, distributed under the
MIT license: https://github.com/FlaUI/FlaUI

Automatic VRChat discovery uses VRChat.OSCQuery 0.0.6, distributed under the
MIT license: https://github.com/vrchat-community/vrc-oscquery-lib
