import importlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import bot


class ControlTest(unittest.TestCase):
    def test_command_normalization(self):
        self.assertEqual(bot.command_of(' /STATUS@ExampleBot '), ('/status', []))
        self.assertEqual(bot.command_of(''), ('', []))

    def test_confirmation_is_required_and_bound_to_chat(self):
        pending = {}
        with patch.object(bot, 'run') as run:
            bot.dispatch('owner', '/restart_adguard', pending)
            run.assert_not_called()
            bot.dispatch('other', '/confirm restart_adguard', pending)
            run.assert_not_called()
            bot.dispatch('owner', '/confirm restart_adguard', pending)
            run.assert_called_once_with('docker', 'restart', bot.ADGUARD_CONTAINER, timeout=60)
            bot.dispatch('owner', '/confirm restart_adguard', pending)
            self.assertEqual(run.call_count, 1)

    def test_expired_or_cancelled_action_does_not_execute(self):
        with patch.object(bot, 'run') as run, patch.object(bot, 'send') as send:
            pending = {'owner': ('reboot', time.monotonic()-61)}
            bot.dispatch('owner', '/confirm reboot', pending)
            bot.dispatch('owner', '/reboot', pending)
            bot.dispatch('owner', '/cancel', pending)
            bot.dispatch('owner', '/confirm reboot', pending)
            run.assert_not_called()
            send.assert_not_called()

    def test_dns_configuration_is_used(self):
        with patch.object(bot, 'ADGUARD_DNS_SERVER', '127.0.0.53'), \
             patch.object(bot, 'run', side_effect=['running|now', '0.0.0.0', '[]']) as run:
            self.assertIn('AdGuard: running', bot.adguard())
            self.assertEqual(run.call_args_list[1].args, ('dig', '+short', '@127.0.0.53', 'doubleclick.net', 'A'))

    def test_environment_overrides(self):
        with tempfile.TemporaryDirectory() as folder:
            env = {'CONTROL_STATE_DIR': folder, 'CONTROL_BACKUP_DIR': folder,
                   'ADGUARD_CONTAINER': 'test-container', 'ADGUARD_CONF_VOLUME': 'test-conf',
                   'ADGUARD_WORK_VOLUME': 'test-work', 'ADGUARD_DNS_SERVER': '127.0.0.53'}
            try:
                with patch.dict(os.environ, env):
                    importlib.reload(bot)
                    self.assertEqual(bot.STATE_DIR, Path(folder))
                    self.assertEqual(bot.BACKUP_ROOT, Path(folder))
                    self.assertEqual(bot.ADGUARD_CONTAINER, 'test-container')
                    self.assertEqual(bot.ADGUARD_CONF_VOLUME, 'test-conf')
                    self.assertEqual(bot.ADGUARD_WORK_VOLUME, 'test-work')
                    self.assertEqual(bot.ADGUARD_DNS_SERVER, '127.0.0.53')
            finally:
                importlib.reload(bot)

    def test_poll_offset_is_persisted_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(bot, 'STATE_DIR', Path(folder)), \
             patch.object(bot, 'STATE_FILE', Path(folder)/'state.json'):
            self.assertIsNone(bot.load_offset())
            bot.save_offset(123)
            self.assertEqual(bot.load_offset(), 123)
            self.assertEqual(bot.STATE_FILE.stat().st_mode & 0o777, 0o600)

    def test_unauthorized_message_never_reaches_dispatch(self):
        with tempfile.TemporaryDirectory() as folder, (Path(folder)/'bot.lock').open('w') as lock, \
             patch.object(Path, 'open', return_value=lock), patch.object(bot, 'STATE_DIR', Path(folder)), \
             patch.object(bot, 'TOKEN', 'synthetic-token'), patch.object(bot, 'ADMIN_CHAT_ID', 'owner'), \
             patch.object(bot, 'load_offset', return_value=0), patch.object(bot, 'save_offset'), \
             patch.object(bot, 'dispatch') as dispatch, \
             patch.object(bot, 'api', side_effect=[True, [{'update_id': 1, 'message': {'chat': {'id': 'stranger'}, 'text': '/reboot'}}], KeyboardInterrupt]):
            with self.assertRaises(KeyboardInterrupt):
                bot.main()
            dispatch.assert_not_called()
