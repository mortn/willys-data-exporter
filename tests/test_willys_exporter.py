import base64
import hashlib
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import willys_exporter
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


class FakeResponse:
    def __init__(self, payload, content=b'', headers=None):
        self.payload = payload
        self.content = content
        self.headers = headers or {}

    def json(self):
        return self.payload

    def raise_for_status(self):
        return None


class FakeFetchSession:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append((url, params, timeout))
        return FakeResponse(self.pages[int(params['currentPage'])])


class FakeLoginSession:
    def __init__(self, payload):
        self.payload = payload
        self.headers = {}

    def post(self, url, json, timeout):
        return FakeResponse(self.payload)


class FakeReceiptSession:
    def __init__(self):
        self.calls = []

    def get(self, url, params, headers, timeout):
        self.calls.append((url, params, headers, timeout))
        return FakeResponse({}, content=b'%PDF-1.5 fake receipt')


class WillysExporterTests(unittest.TestCase):
    def test_encryption_payload_can_be_decrypted(self):
        encoded, key = willys_exporter._encrypt_login_value('test-value')
        iv_hex, salt_hex, ciphertext = base64.b64decode(encoded).decode().split('::')
        derived_key = hashlib.pbkdf2_hmac(
            'sha1', key.encode(), bytes.fromhex(salt_hex), 1_000, dklen=16
        )
        decryptor = Cipher(
            algorithms.AES(derived_key), modes.CBC(bytes.fromhex(iv_hex))
        ).decryptor()
        padded = decryptor.update(base64.b64decode(ciphertext)) + decryptor.finalize()
        padding_length = padded[-1]

        self.assertEqual(padded[:-padding_length].decode(), 'test-value')
        self.assertEqual(len(key), 16)
        self.assertEqual(len(bytes.fromhex(iv_hex)), 16)
        self.assertEqual(len(bytes.fromhex(salt_hex)), 16)

    def test_fetch_month_combines_pages(self):
        pages = [
            {
                'loyaltyTransactionsInPage': [{'id': 'first'}],
                'paginationData': {'numberOfPages': 2},
            },
            {
                'loyaltyTransactionsInPage': [{'id': 'second'}],
                'paginationData': {'numberOfPages': 2},
            },
        ]
        session = FakeFetchSession(pages)

        result = willys_exporter.fetch_month(
            session, date(2025, 11, 1), date(2025, 11, 30)
        )

        self.assertEqual(
            result['loyaltyTransactionsInPage'],
            [{'id': 'first'}, {'id': 'second'}],
        )
        self.assertEqual(result['paginationData']['numberOfPages'], 1)
        self.assertEqual(result['paginationData']['totalNumberOfResults'], 2)
        self.assertEqual([call[1]['currentPage'] for call in session.calls], ['0', '1'])

    def test_fetch_month_rejects_invalid_pagination(self):
        for invalid_pagination in (None, ['not', 'an', 'object']):
            with self.subTest(invalid_pagination=invalid_pagination):
                session = FakeFetchSession(
                    [
                        {
                            'loyaltyTransactionsInPage': [],
                            'paginationData': invalid_pagination,
                        }
                    ]
                )

                with self.assertRaisesRegex(RuntimeError, 'invalid pagination'):
                    willys_exporter.fetch_month(
                        session, date(2025, 11, 1), date(2025, 11, 30)
                    )

    def test_named_profile_uses_profile_credentials_and_directories(self):
        with patch.dict(
            os.environ,
            {
                'WILLYS_USER2_USERNAME': 'user2-username',
                'WILLYS_USER2_PASSWORD': 'user2-password',
            },
            clear=True,
        ):
            self.assertEqual(
                willys_exporter._profile_credentials('user2'),
                ('user2-username', 'user2-password'),
            )

        data_dir, receipts_dir = willys_exporter._profile_directories('user2')
        self.assertEqual(data_dir.name, 'user2')
        self.assertEqual(receipts_dir.name, 'user2')

    def test_invalid_profile_is_rejected(self):
        with self.assertRaises(ValueError):
            willys_exporter._normalize_profile('../user2')

    def test_login_rejects_non_object_response(self):
        fake_session = FakeLoginSession([])
        with patch.dict(
            os.environ,
            {'WILLYS_USERNAME': 'user', 'WILLYS_PASSWORD': 'password'},
        ), patch.object(
            willys_exporter, '_build_session', return_value=fake_session
        ):
            with self.assertRaisesRegex(RuntimeError, 'unexpected login response'):
                willys_exporter.login()

    def test_download_receipts_saves_pdf(self):
        session = FakeReceiptSession()
        transaction = {
            'bookingDate': 1764345857936,
            'digitalReceiptAvailable': True,
            'digitalReceiptReference': 'receipt:2025/11/28',
            'memberCardNumber': 'card',
            'receiptSource': 'aws',
            'storeCustomerId': '2182',
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            downloaded, skipped = willys_exporter.download_receipts(
                session,
                [transaction],
                '2025-11',
                Path(temporary_directory),
            )
            receipt_files = list(Path(temporary_directory).rglob('*.pdf'))
            receipt_bytes = receipt_files[0].read_bytes()

        self.assertEqual((downloaded, skipped), (1, 0))
        self.assertEqual(len(receipt_files), 1)
        self.assertTrue(receipt_bytes.startswith(b'%PDF-'))
        self.assertIn('2025-11-28', session.calls[0][1]['date'])
        self.assertEqual(session.calls[0][2]['Accept'], 'application/pdf')

    def test_main_reports_write_errors(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory)
            month = (date(2025, 11, 1), date(2025, 11, 1), date(2025, 11, 30))
            with patch.object(willys_exporter, 'DATA_DIR', data_dir), patch.object(
                willys_exporter, 'login', return_value=object()
            ), patch.object(
                willys_exporter, 'month_ranges', return_value=iter([month])
            ), patch.object(
                willys_exporter,
                'fetch_month',
                return_value={'loyaltyTransactionsInPage': []},
            ), patch.object(
                willys_exporter,
                'write_json',
                side_effect=OSError('disk full'),
            ), patch.object(sys, 'argv', ['willys_exporter.py']):
                self.assertEqual(willys_exporter.main(), 1)


if __name__ == '__main__':
    unittest.main()
