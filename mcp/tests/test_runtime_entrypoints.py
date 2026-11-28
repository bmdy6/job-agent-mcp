"""Deployment/launch imports run against temporary homes, without launchctl or vault access."""
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def script(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT/'scripts'/(name+'.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RuntimeEntrypointTests(unittest.TestCase):
    def test_daily_launch_import_and_legacy_env_precedence(self):
        launcher = script('daily-launch')
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            secret = home/'daily.env.json'
            secret.write_text(json.dumps({'UNIT_EXISTING':'file','UNIT_ADDED':'file'}))
            secret.chmod(0o600)
            with patch.dict(os.environ, JOB_AGENT_HOME=directory, UNIT_EXISTING='environment'), \
                 patch('job_agent.cli.main', return_value=0) as cli, \
                 patch.object(signal, 'signal'), patch('sys.argv', ['daily-launch.py','--max-per-platform','1']):
                self.assertEqual(launcher.main(),0)
                self.assertEqual(os.environ['UNIT_EXISTING'],'environment')
                self.assertEqual(os.environ['UNIT_ADDED'],'file')
                cli.assert_called_once_with(['daily','--once','--scheduled','--max-per-platform','1'])
                secret.chmod(0o644)
                with self.assertRaisesRegex(ValueError,'private'):
                    launcher.main()
                self.assertEqual(cli.call_count,1)

    def test_monitor_and_heartbeat_exec_installed_cli(self):
        for name, args in [('monitor-launch',['monitor']),('heartbeat-launch',['heartbeat','--once'])]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                home=Path(directory)
                vault=home/'.config/ai-vault/vault.json'
                vault.parent.mkdir(parents=True)
                vault.write_text('{"test":{"UNIT_TOKEN":{"value":"synthetic"}}}')
                (home/'config.yaml').write_text('notify:\n  channels:\n    test: {type: telegram, enabled: true, bot_token_env: UNIT_TOKEN}\n')
                launcher=script(name)
                with patch.dict(os.environ,JOB_AGENT_HOME=directory), patch.object(Path,'home',return_value=home), \
                     patch.object(os,'execve') as execute:
                    launcher.main()
                    executable, argv, env=execute.call_args.args
                    self.assertEqual(executable,str(ROOT/'.venv/bin/job-agent'))
                    self.assertEqual(argv,[executable,*args])
                    self.assertEqual(env['UNIT_TOKEN'],'synthetic')

    def test_build_uses_committed_lock_and_noneditable_runtime_environment(self):
        deploy=script('deploy')
        archive=io.BytesIO()
        with tarfile.open(fileobj=archive,mode='w') as stream:
            for name in ['mcp/pyproject.toml','mcp/uv.lock']:
                data=(ROOT/name).read_bytes()
                info=tarfile.TarInfo(name);info.size=len(data)
                stream.addfile(info,io.BytesIO(data))
        with tempfile.TemporaryDirectory() as directory:
            home=Path(directory)
            app=deploy.Deployer(ROOT,home,home/'agents',object())
            with patch.object(deploy.subprocess,'check_output',return_value=archive.getvalue()), \
                 patch.object(deploy.subprocess,'run') as run, patch.object(app,'python',return_value='6'):
                runtime=app.build('a'*40)
                run.assert_called_once()
                self.assertEqual(run.call_args.args[0],['uv','sync','--project',str(runtime/'mcp'),'--frozen','--no-editable'])
                self.assertEqual(run.call_args.kwargs['env']['UV_PROJECT_ENVIRONMENT'],str(runtime/'.venv'))
                self.assertTrue(json.loads((runtime/'deployment.json').read_text())['ready'])
