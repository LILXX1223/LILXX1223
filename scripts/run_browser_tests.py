"""Run browser E2E against disposable SQLite and isolated local servers only."""
import os
import json
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parents[1]
PYTHON=ROOT/'.venv/bin/python'
NODE=ROOT/'.tools/node/bin/node'
VITE=ROOT/'frontend/node_modules/vite/bin/vite.js'
PLAYWRIGHT=ROOT/'frontend/node_modules/@playwright/test/cli.js'


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        return sock.getsockname()[1]


def wait_ready(url,process):
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        if process.poll() is not None: raise RuntimeError('测试服务提前退出，请检查运行日志。')
        try:
            with urlopen(url,timeout=1) as response:
                if response.status<500:return
        except HTTPError as exc:
            if exc.code<500:return
        except (URLError,TimeoutError,OSError):pass
        time.sleep(.2)
    raise RuntimeError('测试服务启动超时。')


def main():
    arguments=sys.argv[1:]
    real_ai='--real-ai' in arguments
    current_scenes='--current-scenes' in arguments
    mock_ai='--mock-ai' in arguments
    arguments=[arg for arg in arguments if arg not in ('--real-ai','--current-scenes','--mock-ai')]
    if real_ai and mock_ai: raise RuntimeError('真实AI测试与模拟AI测试不能同时启用。')
    if real_ai and not arguments:
        arguments=['e2e/real-ai-allocation.spec.js']
    with tempfile.TemporaryDirectory(prefix='icu-browser-e2e-') as temporary:
        backend,frontend=free_port(),free_port()
        while frontend==backend:frontend=free_port()
        env=dict(os.environ, DJANGO_SETTINGS_MODULE='icu_scheduler.e2e_settings',
            ICU_E2E_DATABASE_PATH=str(Path(temporary)/'browser-tests.sqlite3'), DJANGO_DEBUG='true',
            DJANGO_ALLOWED_HOSTS='127.0.0.1,localhost,testserver',
            DJANGO_CSRF_TRUSTED_ORIGINS=f'http://127.0.0.1:{frontend}',
            VITE_API_TARGET=f'http://127.0.0.1:{backend}',VITE_PORT=str(frontend),
            E2E_BASE_URL=f'http://127.0.0.1:{frontend}',ICU_E2E_ISOLATED='1',E2E_ADMIN_USER='e2e_admin',
            ICU_E2E_REAL_AI='1' if real_ai else '0',
            DEMO_PASSWORD='DemoICU!2026',E2E_PASSWORD='DemoICU!2026',
            E2E_ADMIN_PASSWORD=secrets.token_urlsafe(24),ICU_AI_ALLOW_MOCK='true' if mock_ai else 'false',
            PLAYWRIGHT_HTML_OUTPUT_DIR=str(Path(temporary)/'playwright-report'),
            AI_HOSPITAL_AUTHORIZED='false',AI_PERSONNEL_EXTERNAL_AUTHORIZED='false',
            PATH=str(ROOT/'.tools/node/bin')+os.pathsep+os.environ.get('PATH',''))
        subprocess.run([str(PYTHON),str(ROOT/'manage.py'),'migrate','--noinput'],cwd=ROOT,env=env,check=True,stdout=subprocess.DEVNULL)
        subprocess.run([str(PYTHON),str(ROOT/'manage.py'),'seed_demo'],cwd=ROOT,env=env,check=True)
        if real_ai:
            # Read only the encrypted singleton configuration, never clinical data.
            # Import via Django ORM into the disposable database and never print it.
            source=ROOT/'db.sqlite3'
            with sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True) as conn:
                row=conn.execute('SELECT provider, base_url, model, encrypted_key FROM scheduling_aiserviceconfiguration WHERE id=1').fetchone()
                scenes=conn.execute('SELECT scene,model,thinking,max_tokens,timeout_seconds,stream,output_format,revision FROM scheduling_aisceneconfiguration').fetchall() if current_scenes else []
            if not row or not row[3]:
                raise RuntimeError('当前项目未保存可用的 AI 服务配置，无法执行真实 AI 联调。')
            import_script=(
                "import json,os; from scheduling.models import AIServiceConfiguration; "
                "from scheduling.services.ai_configuration import secret_value; "
                "v=json.loads(os.environ.pop('ICU_E2E_ENCRYPTED_CONFIG')); "
                "AIServiceConfiguration.objects.create(pk=1,provider=v[0],base_url=v[1],model=v[2],encrypted_key=v[3]); "
                "assert secret_value(), '无法解密当前 AI 配置'")
            import_env={**env,'ICU_E2E_ENCRYPTED_CONFIG':json.dumps(row)}
            subprocess.run([str(PYTHON),str(ROOT/'manage.py'),'shell','-c',import_script],cwd=ROOT,env=import_env,check=True)
            if scenes:
                scene_script=("import json,os; from scheduling.models import AISceneConfiguration; "
                    "rows=json.loads(os.environ.pop('ICU_E2E_SCENES')); "
                    "[AISceneConfiguration.objects.filter(scene=r[0]).update(model=r[1],thinking=r[2],max_tokens=r[3],timeout_seconds=r[4],stream=bool(r[5]),output_format=r[6],revision=r[7]) for r in rows]")
                subprocess.run([str(PYTHON),str(ROOT/'manage.py'),'shell','-c',scene_script],cwd=ROOT,
                    env={**env,'ICU_E2E_SCENES':json.dumps(scenes)},check=True)
            print('真实 AI 联调已启用：仅向模型发送隔离库中的合成资料。',flush=True)
        admin_script="import os; from django.contrib.auth import get_user_model; get_user_model().objects.create_superuser(os.environ['E2E_ADMIN_USER'],password=os.environ['E2E_ADMIN_PASSWORD'])"
        subprocess.run([str(PYTHON),str(ROOT/'manage.py'),'shell','-c',admin_script],cwd=ROOT,env=env,check=True)
        services=[]
        try:
            with open(Path(temporary)/'django.log','wb') as backend_log,open(Path(temporary)/'vite.log','wb') as frontend_log:
                services.append(subprocess.Popen([str(PYTHON),str(ROOT/'manage.py'),'runserver',f'127.0.0.1:{backend}','--noreload'],cwd=ROOT,env=env,stdout=backend_log,stderr=subprocess.STDOUT))
                services.append(subprocess.Popen([str(NODE),str(VITE),'--host','127.0.0.1'],cwd=ROOT/'frontend',env=env,stdout=frontend_log,stderr=subprocess.STDOUT))
                wait_ready(f'http://127.0.0.1:{backend}/admin/',services[0])
                wait_ready(f'http://127.0.0.1:{frontend}/',services[1])
                print('浏览器测试使用临时独立数据库和服务；真实 AI 模式仅只读复制业务库中的加密配置。' if real_ai
                      else '浏览器测试使用临时独立数据库和服务，不会连接项目业务库。',flush=True)
                result=subprocess.run([str(NODE),str(PLAYWRIGHT),'test','--output',str(Path(temporary)/'test-results'),*arguments],cwd=ROOT/'frontend',env=env)
                if result.returncode:raise SystemExit(result.returncode)
        finally:
            for service in services:service.terminate()
            for service in services:
                try:service.wait(timeout=5)
                except subprocess.TimeoutExpired:service.kill();service.wait()


if __name__=='__main__':main()
