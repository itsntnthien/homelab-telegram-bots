"""Run offline suites separately so the two bot.py modules cannot collide."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--require-linux', action='store_true')
    args = parser.parse_args()
    if args.require_linux and sys.platform != 'linux':
        raise SystemExit('Full verification requires Linux for fcntl and /proc')
    env = dict(os.environ, PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1')
    for key in list(env):
        if key.startswith(('TELEGRAM_', 'EXNESS_', 'CONTROL_', 'ADGUARD_', 'MARKET_', 'INTRADAY_', 'PINE_', 'RELEASE1_')) or key in {
            'SIGNAL_ENGINE', 'SYMBOL', 'NEWS_DB', 'TRADES_DB', 'BOT_HEALTH_PORT', 'FRED_API_KEY', 'PARITY_APPROVED'}:
            env.pop(key)
    env.update(DRY_RUN='true', PARITY_APPROVED='false')
    with tempfile.TemporaryDirectory() as folder:
        env['NEWS_DB'] = str(Path(folder)/'news.db')
        commands = [([sys.executable, '-m', 'unittest', 'discover', '-s', '.', '-p', 'test_*.py', '-v'], ROOT/'apps/trading-bot')]
        if sys.platform == 'linux':
            commands += [([sys.executable, 'bot.py', '--self-test'], ROOT/'apps/control-bot'),
                         ([sys.executable, '-m', 'unittest', 'discover', '-v'], ROOT/'apps/control-bot')]
        else:
            print('SKIP: control bot runtime tests require Linux.', flush=True)
        node = shutil.which('node')
        if not node:
            raise SystemExit('Node.js 22+ is required for transport tests')
        commands += [([node, '--check', 'exness_transport.mjs'], ROOT/'apps/trading-bot'),
                     ([node, '--test', 'test_exness_signer.mjs', 'test_exness_sessions.mjs'], ROOT/'apps/trading-bot'),
                     ([sys.executable, 'scripts/demo.py'], ROOT),
                     ([sys.executable, 'scripts/verify_package.py'], ROOT)]
        for command, cwd in commands:
            print(f'\n== {cwd.relative_to(ROOT)}: {" ".join(command[1:])} ==', flush=True)
            subprocess.run(command, cwd=cwd, env=env, check=True)


if __name__ == '__main__':
    main()
