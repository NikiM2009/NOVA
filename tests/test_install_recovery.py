import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import httpx

from nova.agent import Engine


class InstallRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.engine=Engine(Path(self.temp.name)/'agent')
        self.spec=dict(name='Velocity',software='velocity',version='3.4.0',port=25565,memory=1024)
        self.java=patch('nova.agent.shutil.which',return_value='java')
        self.version=patch('nova.agent.subprocess.run',return_value=subprocess.CompletedProcess([],0,stdout='',stderr='openjdk version "21.0.6"'))
        self.java.start()
        self.version.start()

    def tearDown(self):
        self.java.stop()
        self.version.stop()
        self.engine.close()
        self.temp.cleanup()

    def archive(self,software,version,target):
        with zipfile.ZipFile(target,'w') as jar:
            jar.writestr('META-INF/MANIFEST.MF','Manifest-Version: 1.0\n')
        return 123

    def test_failed_download_can_retry_without_replacing_velocity_configuration(self):
        root=self.engine.folder('velocity')
        root.mkdir(parents=True)
        config='config-version = "2.7"\nmotd = "My existing network"\n'
        (root/'velocity.toml').write_text(config,encoding='utf-8')
        with patch('nova.agent.catalog.download',side_effect=httpx.ConnectError('Download failed')):
            with self.assertRaises(httpx.ConnectError):
                self.engine.install('velocity',self.spec)
        self.assertNotIn('velocity',self.engine.servers)
        with patch('nova.agent.catalog.download',side_effect=self.archive):
            result=self.engine.install('velocity',dict(self.spec,recover_existing=True))
        self.assertEqual(result['build'],123)
        self.assertEqual((root/'velocity.toml').read_text(encoding='utf-8'),config)
        restored=Engine(self.engine.root)
        self.assertIn('velocity',restored.servers)

    def test_lost_manifest_can_restore_existing_jar_without_download_or_file_changes(self):
        with patch('nova.agent.catalog.download',side_effect=self.archive):
            self.engine.install('velocity',self.spec)
        root=self.engine.folder('velocity')
        jar_hash=hashlib.sha256((root/'server.jar').read_bytes()).hexdigest()
        config='motd = "Do not overwrite"\n'
        (root/'velocity.toml').write_text(config,encoding='utf-8')
        (root/'plugins').mkdir()
        (root/'plugins'/'keep.txt').write_text('keep',encoding='utf-8')
        self.engine.manifest.unlink()
        recovered=Engine(self.engine.root)
        self.assertNotIn('velocity',recovered.servers)
        with patch('nova.agent.catalog.download') as download:
            recovered.install('velocity',dict(self.spec,recover_existing=True))
            download.assert_not_called()
        self.assertEqual(hashlib.sha256((root/'server.jar').read_bytes()).hexdigest(),jar_hash)
        self.assertEqual((root/'velocity.toml').read_text(encoding='utf-8'),config)
        self.assertEqual((root/'plugins'/'keep.txt').read_text(encoding='utf-8'),'keep')
        self.assertIn('velocity',Engine(self.engine.root).servers)

    def test_invalid_existing_jar_is_not_overwritten(self):
        root=self.engine.folder('velocity')
        root.mkdir(parents=True)
        (root/'server.jar').write_bytes(b'broken download')
        with patch('nova.agent.catalog.download') as download:
            with self.assertRaisesRegex(ValueError,'keine lesbare JAR'):
                self.engine.install('velocity',dict(self.spec,recover_existing=True))
            download.assert_not_called()
        self.assertEqual((root/'server.jar').read_bytes(),b'broken download')


if __name__=='__main__':
    unittest.main()
