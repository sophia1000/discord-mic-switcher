import threading
import unittest
from unittest.mock import patch

import mic_switcher as app


class FakeConfig:
    def __init__(self, values=None):
        self.values = values or {}

    def update(self, *_args):
        pass

    def get(self, key, default=None):
        return self.values.get(key, default)


class FakeLogger:
    def log(self, *_args):
        pass


class FakeEndpoint:
    def __init__(self, muted=False, found=True):
        self.is_muted = muted
        self.is_deafened = False
        self.reader_ready = True
        self.mute_param_found = found
        self.deafen_param_found = found
        self.deafen_param_name = "discorddeafen"
        self.targets = []
        self.deafen_targets = []

    def set_mute(self, target):
        self.targets.append(target)
        changed = self.is_muted != target
        self.is_muted = target
        return changed

    def set_deafen(self, target):
        self.deafen_targets.append(target)
        changed = self.is_deafened != target
        self.is_deafened = target
        return changed


def make_system(mode):
    system = app.MicSyncSystem.__new__(app.MicSyncSystem)
    system.config = FakeConfig()
    system.logger = FakeLogger()
    system.discord = FakeEndpoint(False)
    system.vrchat = FakeEndpoint(False)
    system.system_enabled = True
    system.sync_mode = mode
    system.deafen_sync_enabled = True
    system.deafen_sync_mode = app.MODE_DYNAMIC
    system.last_action = ""
    system.gui = None
    system._event_lock = threading.RLock()
    return system


class DirectionTests(unittest.TestCase):
    def test_dynamic_waits_on_initial_vrchat_state(self):
        system = make_system(app.MODE_DYNAMIC)
        system._on_state_change("vrchat", True, initial=True)
        self.assertEqual(system.discord.targets, [])
        self.assertEqual(system.vrchat.targets, [])

    def test_dynamic_discord_change_drives_vrchat_opposite(self):
        system = make_system(app.MODE_DYNAMIC)
        system._on_state_change("discord", False)
        self.assertEqual(system.vrchat.targets, [True])

    def test_dynamic_vrchat_change_drives_discord_opposite(self):
        system = make_system(app.MODE_DYNAMIC)
        system._on_state_change("vrchat", True)
        self.assertEqual(system.discord.targets, [False])

    def test_vrchat_master_corrects_discord(self):
        system = make_system(app.MODE_VRC_MASTER)
        system.vrchat.is_muted = False
        system.discord.is_muted = False  # User just unmuted Discord too.
        system._on_state_change("discord", False)
        self.assertEqual(system.discord.targets, [True])

    def test_discord_master_corrects_vrchat(self):
        system = make_system(app.MODE_DISCORD_MASTER)
        system.discord.is_muted = True
        system.vrchat.is_muted = True
        system._on_state_change("vrchat", True)
        self.assertEqual(system.vrchat.targets, [False])

    def test_deafen_pauses_mic_sync(self):
        system = make_system(app.MODE_DYNAMIC)
        system.discord.is_deafened = True
        system._on_state_change("discord", True)
        self.assertEqual(system.vrchat.targets, [])

    def test_dynamic_discord_deafen_drives_vrchat_parameter(self):
        system = make_system(app.MODE_DYNAMIC)
        system._on_state_change("discord_deafen", True)
        self.assertEqual(system.vrchat.deafen_targets, [True])

    def test_dynamic_vrchat_parameter_drives_discord_deafen(self):
        system = make_system(app.MODE_DYNAMIC)
        system._on_state_change("vrchat_deafen", True)
        self.assertEqual(system.discord.deafen_targets, [True])

    def test_vrchat_master_deafen_corrects_discord(self):
        system = make_system(app.MODE_DYNAMIC)
        system.deafen_sync_mode = app.MODE_VRC_MASTER
        system.vrchat.is_deafened = True
        system._on_state_change("discord_deafen", False)
        self.assertEqual(system.discord.deafen_targets, [True])

    def test_discord_master_deafen_corrects_vrchat_parameter(self):
        system = make_system(app.MODE_DYNAMIC)
        system.deafen_sync_mode = app.MODE_DISCORD_MASTER
        system.discord.is_deafened = True
        system._on_state_change("vrchat_deafen", False)
        self.assertEqual(system.vrchat.deafen_targets, [True])


class EchoSuppressionTests(unittest.TestCase):
    def test_discord_injected_keypress_is_not_reported_as_user_change(self):
        events = []
        handler = app.DiscordHandler("ctrl+shift+m", FakeLogger(), FakeConfig())
        handler.connected = True
        handler.reader_ready = True
        handler.is_muted = False
        handler.set_callback(lambda *event: events.append(event))

        class FakeKeyboard:
            @staticmethod
            def send(_hotkey):
                pass

        with patch.object(app, "keyboard", FakeKeyboard()):
            self.assertTrue(handler.set_mute(True))
        handler._accept_states(True, None)
        self.assertTrue(handler.is_muted)
        self.assertEqual(events, [])

    def test_vrchat_osc_echo_is_not_reported_as_user_change(self):
        events = []
        handler = app.VRChatOSCHandler("127.0.0.1", 9000, 9001, FakeLogger())
        handler.client = type("Client", (), {"send_message": lambda *_args: None})()
        handler.mute_param_found = True
        handler.is_muted = False
        handler.set_callback(lambda *event: events.append(event))
        self.assertTrue(handler.set_mute(True))
        handler._handle_mute(True)
        self.assertEqual(events, [])

    def test_discord_deafen_echo_is_not_reported_as_user_change(self):
        events = []
        config = FakeConfig({"discord_deafen_hotkey": "ctrl+shift+d"})
        handler = app.DiscordHandler("ctrl+shift+m", FakeLogger(), config)
        handler.connected = True
        handler.reader_ready = True
        handler.is_deafened = False
        handler.set_callback(lambda *event: events.append(event))

        class FakeKeyboard:
            @staticmethod
            def send(_hotkey):
                pass

        with patch.object(app, "keyboard", FakeKeyboard()):
            self.assertTrue(handler.set_deafen(True))
        handler._accept_states(None, True)
        self.assertEqual(events, [])

    def test_vrchat_deafen_echo_is_not_reported_as_user_change(self):
        events = []
        handler = app.VRChatOSCHandler("127.0.0.1", 9000, 9001, FakeLogger())
        handler.client = type("Client", (), {"send_message": lambda *_args: None})()
        handler.deafen_param_found = True
        handler.deafen_param_name = "discorddeafen"
        handler.is_deafened = False
        handler.set_callback(lambda *event: events.append(event))
        self.assertTrue(handler.set_deafen(True))
        handler._handle_deafen(True)
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
