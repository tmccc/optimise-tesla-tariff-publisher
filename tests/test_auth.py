import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from auth import read_secret_file, secret_reader_source


class ExplicitSecretTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.token = self.root / 'synthetic-token'
        self.write(b'SYNTHETIC_TEST_TOKEN\n')

    def write(self, value):
        descriptor = os.open(self.token, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(value)

    def read(self, path=None):
        with patch.dict(os.environ, {'TESSIE_TOKEN_FILE': str(path or self.token)}):
            return read_secret_file('TESSIE_TOKEN_FILE')

    def test_explicit_private_file_only(self):
        self.assertEqual(self.read(), 'SYNTHETIC_TEST_TOKEN')

    def test_missing_setting_does_not_search_for_credentials(self):
        with patch.dict(os.environ, {}, clear=True), patch('auth.os.open') as opening:
            with self.assertRaisesRegex(ValueError, '^explicit_secret_file_unavailable$'):
                read_secret_file('TESSIE_TOKEN_FILE')
        opening.assert_not_called()

    def test_public_or_symlink_or_relative_token_file_refused(self):
        self.token.chmod(0o644)
        with self.assertRaises(ValueError): self.read()
        self.token.chmod(0o600)
        link = self.root / 'link'; link.symlink_to(self.token)
        with self.assertRaises(ValueError): self.read(link)
        with self.assertRaises(ValueError): self.read(Path('relative-token'))

    def test_wrong_owner_or_nonregular_file_refused(self):
        real_fstat = os.fstat
        def another_owner(descriptor):
            result = real_fstat(descriptor)
            from types import SimpleNamespace
            return SimpleNamespace(st_mode=result.st_mode, st_uid=os.getuid() + 1, st_size=result.st_size)
        with patch('auth.os.fstat', side_effect=another_owner):
            with self.assertRaises(ValueError): self.read()
        with self.assertRaises(ValueError): self.read(self.root)

    def test_empty_oversized_malformed_and_internal_whitespace_refused(self):
        for value in (b'', b'a' * 8193, b'\xff', b'one\ntwo', b'one two', b'\x00token'):
            with self.subTest(length=len(value)):
                self.write(value)
                with self.assertRaises(ValueError): self.read()

    def test_errors_never_expose_token_or_path(self):
        self.write(b'SYNTHETIC_PRIVATE_VALUE bad')
        try: self.read()
        except ValueError as error:
            message = str(error)
            self.assertNotIn('SYNTHETIC_PRIVATE_VALUE', message)
            self.assertNotIn(str(self.root), message)
        else: self.fail('Invalid token accepted')

    def test_child_source_contains_code_not_token(self):
        source = secret_reader_source()
        self.assertNotIn('SYNTHETIC_TEST_TOKEN', source)
        namespace = {}
        exec(compile(source, '<secret reader>', 'exec'), namespace)
        with patch.dict(os.environ, {'TESSIE_TOKEN_FILE': str(self.token)}):
            self.assertEqual(namespace['read_secret_file']('TESSIE_TOKEN_FILE'), 'SYNTHETIC_TEST_TOKEN')


if __name__ == '__main__': unittest.main()
