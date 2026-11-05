"""Start local development servers with logs; reuse occupied ports without killing processes."""
import json
import os
from pathlib import Path
import socket
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def listening(port):
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(('127.0.0.1', port)) == 0


def main():
    runtime = ROOT / 'runtime'
    runtime.mkdir(exist_ok=True)
    env = {**os.environ, 'PATH': str(ROOT / '.tools/node/bin') + os.pathsep + os.environ.get('PATH', '')}
    servers = [
        ('django', 8000, [str(ROOT / '.venv/bin/python'), str(ROOT / 'manage.py'), 'runserver', '127.0.0.1:8000'], ROOT),
        ('vue', 5173, [str(ROOT / '.tools/node/bin/node'), str(ROOT / 'frontend/node_modules/vite/bin/vite.js'), '--host', '127.0.0.1'], ROOT / 'frontend'),
    ]
    report = []
    for name, port, command, cwd in servers:
        if listening(port):
            report.append({'service': name, 'status': 'port_already_in_use', 'url': f'http://127.0.0.1:{port}/'})
            continue
        for executable in command[:2]:
            if not Path(executable).is_file():
                raise RuntimeError('缺少运行文件：' + executable)
        logfile = runtime / (name + '.log')
        with logfile.open('ab') as output:
            process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        (runtime / (name + '.pid')).write_text(str(process.pid))
        report.append({'service': name, 'status': 'starting', 'pid': process.pid,
            'url': f'http://127.0.0.1:{port}/', 'log': str(logfile)})
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
