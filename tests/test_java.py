"""Actual JVM lifecycle test, on Windows and Linux; no Minecraft downloads."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from nova.agent import Engine


@unittest.skipUnless(shutil.which('java') and (shutil.which('javac') or os.environ.get('NOVA_TEST_COMPILER_JAR')),
                     'Java 21 JDK required (or NOVA_TEST_COMPILER_JAR for a local Eclipse compiler)')
class JavaIntegrationTests(unittest.TestCase):
    def test_real_java_install_console_restart_and_graceful_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = Path(__file__).parent/'fixtures'/'NovaTestServer.java'
            compiler = os.environ.get('NOVA_TEST_COMPILER_JAR')
            command = ['java','-jar',compiler,'-21'] if compiler else ['javac','--release','21']
            subprocess.run(command+['-encoding','UTF-8','-d',str(root),str(source)],check=True,capture_output=True,timeout=60)
            fixture = root/'fixture.jar'
            with zipfile.ZipFile(fixture,'w') as jar:
                jar.writestr('META-INF/MANIFEST.MF','Manifest-Version: 1.0\r\nMain-Class: NovaTestServer\r\n\r\n')
                jar.write(root/'NovaTestServer.class','NovaTestServer.class')
            engine = Engine(root/'agent')
            spec = dict(name='JavaTest',software='paper',version='1.21.4',port=25590,memory=512,accept_eula=True)
            def verified_fixture_download(software,version,target):
                shutil.copyfile(fixture,target)
                return 1
            try:
                with patch('nova.agent.catalog.download',side_effect=verified_fixture_download):
                    engine.install('java-test',spec)
                engine.start('java-test')
                self.assertTrue(engine.running('java-test'))
                engine.command('java-test','say NOVA')
                deadline = time.monotonic()+5
                while time.monotonic()<deadline:
                    if any('Received: say NOVA' in line for line in engine.logs['java-test']):
                        break
                    time.sleep(.05)
                self.assertTrue(any('Received: say NOVA' in line for line in engine.logs['java-test']))
                engine.stop('java-test')
                self.assertEqual(engine.processes['java-test'].returncode,0)
                self.assertTrue(any('stopped cleanly' in line for line in engine.logs['java-test']))
                engine.start('java-test')
                self.assertTrue(engine.running('java-test'))
                engine.close()
                self.assertFalse(engine.running('java-test'))
            finally:
                engine.close()
