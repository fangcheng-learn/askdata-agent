import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts.homenova_phase_e import start


class FrontendRestartTests(unittest.TestCase):
    def test_frontend_restart_retains_backend(self):
        frontend = [123]
        killed = []
        def kill(pid, sig):
            killed.append(pid)
            frontend.clear()
        def launch(*args, **kwargs):
            frontend.append(456)
            return 456
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            stack.enter_context(patch('sys.argv', ['start.py','restart','--frontend-only','--record-dir',tmp]))
            stack.enter_context(patch.object(start,'PRIVATE',Path(tmp)))
            stack.enter_context(patch.object(start,'available',side_effect=lambda url: '8001' in url or bool(frontend)))
            stack.enter_context(patch.object(start,'urlopen',return_value=io.StringIO('{"llm_model":"existing-model"}')))
            stack.enter_context(patch.object(start,'listener',side_effect=lambda port: [789] if port==8001 else list(frontend)))
            stack.enter_context(patch.object(start.os,'kill',side_effect=kill))
            stack.enter_context(patch.object(start,'launch',side_effect=launch))
            stack.enter_context(patch.object(start.subprocess,'check_output',side_effect=lambda command,**kw:
                'next-server' if command[0]=='ps' else 'n'+str(start.FRONTEND)+'\n'))
            with contextlib.redirect_stdout(io.StringIO()): start.main()
            result=json.loads((Path(tmp)/'startup.json').read_text())
            self.assertEqual(killed,[123])
            self.assertEqual(result['started'],{'frontend':456})
            self.assertEqual(result['backend_pids'],[789])

    def test_missing_backend_is_not_started_by_frontend_only(self):
        with tempfile.TemporaryDirectory() as tmp, patch('sys.argv',['start.py','restart','--frontend-only','--record-dir',tmp]), \
             patch.object(start,'PRIVATE',Path(tmp)), patch.object(start,'available',return_value=False), \
             patch.object(start,'launch') as launch, patch.object(start.os,'kill') as kill:
            with self.assertRaisesRegex(RuntimeError,'Backend is unavailable'): start.main()
            launch.assert_not_called()
            kill.assert_not_called()


if __name__=='__main__': unittest.main()
